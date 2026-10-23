import de from "./de.json";
import en from "./en.json";

type Messages = Record<string, string>;

const catalogs: Record<string, Messages> = { de, en };
let active: Messages = en;

export function setLocale(locale: string): void {
  active = catalogs[locale] ?? en;
}

export function t(key: string, params: Record<string, string> = {}): string {
  const template = active[key] ?? key;
  return template.replace(/\{(\w+)\}/g, (_, name: string) => params[name] ?? `{${name}}`);
}
