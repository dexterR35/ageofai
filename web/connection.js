// A single live connection, with bounded retries and detection of a silent server.
export class LiveConnection {
  constructor(url, { onMessage, onState, Socket = WebSocket,
    later = (fn, delay) => setTimeout(fn, delay), cancel = (id) => clearTimeout(id) } = {}) {
    Object.assign(this, { url, onMessage, onState, Socket, later, cancel });
    this.socket = null;
    this.attempt = 0;
    this.stopped = false;
  }

  connect() {
    if (this.stopped) return;
    this.cancel(this.retryTimer);
    this.onState("connecting");
    const socket = this.socket = new this.Socket(this.url);
    this.armWatchdog(socket);
    socket.onmessage = ({ data }) => {
      if (this.socket !== socket) return;
      try {
        const message = JSON.parse(data);
        if (message.type === "resync") { this.retry(); return; }
        this.onMessage(message);
        if (message.type === "frame") {
          this.attempt = 0;
          this.onState("live");
          this.armWatchdog(socket);
        }
      } catch (error) {
        console.error("Could not display a world update", error);
        this.disconnect(socket);
        this.scheduleRetry();
      }
    };
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.disconnect(socket);
      this.scheduleRetry();
    };
    socket.onerror = () => {
      if (this.socket !== socket) return;
      this.disconnect(socket);
      this.scheduleRetry();
    };
  }

  armWatchdog(socket) {
    this.cancel(this.watchdog);
    this.watchdog = this.later(() => {
      if (this.socket !== socket) return;
      this.disconnect(socket);
      this.scheduleRetry();
    }, 30000);
  }

  disconnect(socket = this.socket) {
    this.cancel(this.watchdog);
    if (socket) {
      socket.onmessage = socket.onclose = socket.onerror = null;
      socket.close();
    }
    this.socket = null;
  }

  scheduleRetry() {
    if (this.stopped) return;
    this.cancel(this.retryTimer);
    const delay = Math.min(30000, 1000 * 2 ** Math.min(this.attempt++, 5));
    this.onState("reconnecting", delay);
    this.retryTimer = this.later(() => this.connect(), delay);
  }

  retry() {
    this.cancel(this.retryTimer);
    this.disconnect();
    this.connect();
  }

  stop() {
    this.stopped = true;
    this.cancel(this.retryTimer);
    this.disconnect();
  }
}
