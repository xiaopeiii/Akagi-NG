import type { Protocol } from 'devtools-protocol';
import { BrowserWindow } from 'electron';
import type { WebContents } from 'electron';

type AutoPlayStep =
  | { op: 'delay'; ms: number }
  | { op: 'move'; x16: number; y9: number }
  | { op: 'click'; x16: number; y9: number };

type AutoPlayPayload = { seq?: number; steps: AutoPlayStep[] };

export class GameHandler {
  private attached = false;
  private readonly BACKEND_API: string;
  private autoplaySeq = 0;
  private autoplayChain: Promise<void> = Promise.resolve();

  constructor(
    private webContents: WebContents,
    apiBase: string,
  ) {
    this.BACKEND_API = `${apiBase}/api/ingest`;
  }

  public async attach() {
    if (this.attached || this.webContents.isDestroyed()) return;

    try {
      // 1. Listen for process issues
      this.webContents.on('render-process-gone', (_event, details) => {
        console.error(
          `[GameHandler] Renderer process gone: ${details.reason} (${details.exitCode})`,
        );
        this.attached = false;
      });

      this.webContents.on('did-start-navigation', (_event, url, isInPlace, isMainFrame) => {
        if (isMainFrame && !isInPlace) {
          console.info(`[GameHandler] Main frame navigating to: ${url}`);
        }
      });

      // 2. Auto re-attach when page reloads or navigates
      this.webContents.on('did-finish-load', async () => {
        if (!this.attached && !this.webContents.isDestroyed()) {
          setTimeout(() => this.tryAttach(), 500);
        }
      });

      // 3. Initial attachment
      await this.tryAttach();
    } catch (err) {
      console.error('[GameHandler] Setup failed:', err);
    }
  }

  private async tryAttach() {
    if (this.attached || this.webContents.isDestroyed()) return;

    try {
      if (this.webContents.debugger.isAttached()) {
        this.attached = true;
        return;
      }

      this.webContents.debugger.attach('1.3');
      this.attached = true;

      this.webContents.debugger.on('detach', (_event, reason) => {
        console.warn('[GameHandler] Debugger detached:', reason);
        this.attached = false;

        // If it was a target-closed (e.g. process swap), we don't send to backend yet,
        // just let did-finish-load or other events trigger re-attach.
        if (reason !== 'target_closed') {
          this.sendToBackend({
            source: 'electron',
            type: 'debugger_detached',
            reason: reason,
            time: Date.now() / 1000,
          });
        }
      });

      this.webContents.debugger.on('message', this.handleDebuggerMessage.bind(this));

      // Wrap command in try-catch to avoid crashing if target closes mid-flight
      try {
        await this.webContents.debugger.sendCommand('Network.enable');
      } catch (cmdErr) {
        console.warn('[GameHandler] Could not enable Network:', cmdErr);
      }
    } catch (e) {
      const error = e as Error;
      console.error('[GameHandler] Attach failed:', error.message);
      this.attached = false;
    }
  }

  public detach() {
    if (this.attached) {
      this.webContents.debugger.detach();
      this.attached = false;
    }
  }

  public async dispatchAutoplaySteps(payload: unknown): Promise<boolean> {
    if (this.webContents.isDestroyed()) return false;

    const p = payload as Partial<AutoPlayPayload> | null;
    if (!p || !Array.isArray(p.steps) || p.steps.length === 0) return false;

    const seq = typeof p.seq === 'number' ? p.seq : 0;
    if (seq && seq <= this.autoplaySeq) return false;
    if (seq) this.autoplaySeq = seq;

    // Serialize step execution so we never interleave click sequences.
    this.autoplayChain = this.autoplayChain
      .then(() => this.runAutoplaySteps(p.steps as AutoPlayStep[]))
      .catch((err) => console.error('[GameHandler] autoplayChain error:', err));

    return true;
  }

  private async sleep(ms: number) {
    await new Promise((resolve) => setTimeout(resolve, ms));
  }

  private async getGameRect(): Promise<{ left: number; top: number; width: number; height: number }> {
    try {
      const result = (await this.webContents.executeJavaScript(
        `(() => {
          const canvases = Array.from(document.querySelectorAll('canvas'));
          let best = null;
          let bestRect = null;
          let bestArea = 0;
          for (const c of canvases) {
            const r = c.getBoundingClientRect();
            const area = r.width * r.height;
            if (area > bestArea) {
              best = c;
              bestRect = r;
              bestArea = area;
            }
          }
          const r = bestRect || { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
          return { left: r.left, top: r.top, width: r.width, height: r.height };
        })()`,
        true,
      )) as { left?: unknown; top?: unknown; width?: unknown; height?: unknown };
      const left = Number(result?.left);
      const top = Number(result?.top);
      const w = Number(result?.width);
      const h = Number(result?.height);
      if (
        Number.isFinite(left) &&
        Number.isFinite(top) &&
        Number.isFinite(w) &&
        Number.isFinite(h) &&
        w > 0 &&
        h > 0
      ) {
        return { left, top, width: w, height: h };
      }
    } catch (e) {
      console.warn('[GameHandler] Failed to read game rect:', e);
    }

    // Fallback: window bounds (may include frame chrome, but better than nothing)
    try {
      const b = BrowserWindow.fromWebContents(this.webContents)?.getContentBounds();
      if (b && b.width > 0 && b.height > 0) return { left: 0, top: 0, width: b.width, height: b.height };
    } catch {
      // ignore
    }
    return { left: 0, top: 0, width: 1280, height: 720 };
  }

  private toViewportPx(x16: number, y9: number, rect: { left: number; top: number; width: number; height: number }) {
    const x = rect.left + (x16 / 16) * rect.width;
    const y = rect.top + (y9 / 9) * rect.height;
    return { x, y };
  }

  private async dispatchMouseMove(x: number, y: number) {
    await this.webContents.debugger.sendCommand('Input.dispatchMouseEvent', {
      type: 'mouseMoved',
      x,
      y,
    });
  }

  private async dispatchMouseClick(x: number, y: number) {
    await this.webContents.debugger.sendCommand('Input.dispatchMouseEvent', {
      type: 'mousePressed',
      x,
      y,
      button: 'left',
      clickCount: 1,
    });
    // Slightly longer press helps Majsoul register clicks more reliably under load.
    await this.sleep(120);
    await this.webContents.debugger.sendCommand('Input.dispatchMouseEvent', {
      type: 'mouseReleased',
      x,
      y,
      button: 'left',
      clickCount: 1,
    });
  }

  private async runAutoplaySteps(steps: AutoPlayStep[]) {
    await this.tryAttach();
    if (!this.webContents.debugger.isAttached()) return;

    const rect = await this.getGameRect();

    for (const step of steps) {
      if (this.webContents.isDestroyed()) return;

      if (step.op === 'delay') {
        const ms = Math.max(0, Math.min(5000, Number(step.ms) || 0));
        await this.sleep(ms);
        continue;
      }

      const { x, y } = this.toViewportPx(Number(step.x16) || 0, Number(step.y9) || 0, rect);
      // Small jitter keeps movement less robotic but still accurate.
      const jx = x + (Math.random() * 2 - 1);
      const jy = y + (Math.random() * 2 - 1);

      if (step.op === 'move') {
        await this.dispatchMouseMove(jx, jy);
        continue;
      }

      if (step.op === 'click') {
        await this.dispatchMouseMove(jx, jy);
        await this.dispatchMouseClick(jx, jy);
        continue;
      }
    }
  }

  private async handleDebuggerMessage(_event: unknown, method: string, params: unknown) {
    if (method === 'Network.webSocketCreated') {
      const p = params as Protocol.Network.WebSocketCreatedEvent;
      this.sendToBackend({
        source: 'electron',
        type: 'websocket_created',
        requestId: p.requestId,
        url: p.url,
        time: Date.now() / 1000,
      });
    } else if (method === 'Network.webSocketClosed') {
      const p = params as Protocol.Network.WebSocketClosedEvent;
      this.sendToBackend({
        source: 'electron',
        type: 'websocket_closed',
        requestId: p.requestId,
        time: Date.now() / 1000,
      });
    } else if (method === 'Network.webSocketFrameReceived') {
      this.handleWebSocketFrame(params as Protocol.Network.WebSocketFrameReceivedEvent, 'inbound');
    } else if (method === 'Network.webSocketFrameSent') {
      this.handleWebSocketFrame(params as Protocol.Network.WebSocketFrameSentEvent, 'outbound');
    } else if (method === 'Network.responseReceived') {
      await this.handleResponseReceived(params as Protocol.Network.ResponseReceivedEvent);
    }
  }

  private handleWebSocketFrame(
    params: Protocol.Network.WebSocketFrameReceivedEvent | Protocol.Network.WebSocketFrameSentEvent,
    direction: 'inbound' | 'outbound',
  ) {
    const { requestId, response } = params;

    let data = '';
    let opcode = -1;

    // Unified handling for both inbound and outbound frames
    // CDP puts payloadData inside the 'response' object for both events
    if (response && response.payloadData) {
      data = response.payloadData;
      opcode = response.opcode;
    } else {
      // Fallback: Check if payloadData is at the top level (older CDP or different backend)
      const p = params as unknown as Record<string, unknown>;
      if (typeof p.payloadData === 'string') {
        data = p.payloadData;
        opcode = typeof p.opcode === 'number' ? p.opcode : 2;
      } else {
        return;
      }
    }

    const payload = {
      source: 'electron',
      type: 'websocket',
      requestId: requestId,
      direction: direction,
      data: data, // Base64 string
      opcode: opcode,
      time: Date.now() / 1000,
    };

    this.sendToBackend(payload);
  }

  private async handleResponseReceived(params: Protocol.Network.ResponseReceivedEvent) {
    const { response } = params;

    if (response.url && response.url.includes('liqi.json')) {
      try {
        // Use fetch instead of CDP getResponseBody to avoid "No resource with given identifier" errors
        const res = await fetch(response.url);
        if (res.ok) {
          const text = await res.text();
          this.sendToBackend({
            source: 'electron',
            type: 'liqi_definition',
            data: text, // Send raw text (or JSON string)
            url: response.url,
          });
        } else {
          console.error(`[GameHandler] Failed to fetch liqi.json: HTTP ${res.status}`);
        }
      } catch (e) {
        console.error('[GameHandler] Failed to fetch liqi.json manually:', e);
      }
    }
  }

  private sendToBackend(data: unknown) {
    fetch(this.BACKEND_API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }).catch((err) => {
      console.error('[GameHandler] Failed to send to backend:', err);
    });
  }
}
