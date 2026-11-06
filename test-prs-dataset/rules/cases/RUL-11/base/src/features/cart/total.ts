export type Line = { sku: string; price: number; quantity: number };

const TAX_RATE = 0.2;

export function cartTotal(items: Line[]): number {
  const subtotal = items.reduce((sum, item) => sum + item.price * item.quantity, 0);
  const tax = subtotal * TAX_RATE;
  return Math.round((subtotal + tax) * 100) / 100;
}

export function recalculateLater(items: Line[], onTotal: (total: number) => void): void {
  setTimeout(() => onTotal(cartTotal(items)), 100);
}
