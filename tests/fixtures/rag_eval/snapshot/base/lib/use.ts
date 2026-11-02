import { persist } from "./index";
import { save } from "./store";

export function run(): string {
  return persist("a") + save("b");
}
