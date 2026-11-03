export type Zone = "domestic" | "europe" | "overseas";

export interface CartLine {
  sku: string;
  quantity: number;
  unitPriceCents: number;
  weightKg: number;
}
