import { useEffect } from "react";

export type NotificationSource = {
  subscribe(listener: (message: string) => void): void;
  unsubscribe(listener: (message: string) => void): void;
};

export function useNotifications(
  source: NotificationSource,
  onMessage: (message: string) => void,
): void {
  useEffect(() => {
    source.subscribe(onMessage);
    return () => source.unsubscribe(onMessage);
  }, [source, onMessage]);
}
