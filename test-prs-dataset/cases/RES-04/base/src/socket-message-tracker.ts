export class SocketMessageTracker {
  private readonly socket: EventTarget;
  private readonly onMessage: EventListener;
  private readonly onClose: EventListener;
  messageCount = 0;
  isClosed = false;

  constructor(socket: EventTarget) {
    this.socket = socket;
    this.onMessage = () => {
      this.messageCount += 1;
    };
    this.onClose = () => {
      this.isClosed = true;
    };
    this.socket.addEventListener("message", this.onMessage);
    this.socket.addEventListener("close", this.onClose);
  }

  dispose(): void {
    this.socket.removeEventListener("message", this.onMessage);
    this.socket.removeEventListener("close", this.onClose);
  }
}
