import { subtotalCents, totalWeightKg } from "./cart";
import { formatCents } from "./format";
import { shippingCents } from "./shipping";
import type { CartLine, Zone } from "./types";

export interface Quote {
  subtotalCents: number;
  shippingCents: number;
  totalCents: number;
  label: string;
}

export function quoteOrder(lines: CartLine[], zone: Zone): Quote {
  const subtotal = subtotalCents(lines);
  const shipping = shippingCents(totalWeightKg(lines), zone);
  const totalCents = subtotal + shipping;
  return {
    subtotalCents: subtotal,
    shippingCents: shipping,
    totalCents,
    label: `Total ${formatCents(totalCents)}`,
  };
}
