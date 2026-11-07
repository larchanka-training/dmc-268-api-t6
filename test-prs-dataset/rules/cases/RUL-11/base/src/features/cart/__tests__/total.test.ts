import { describe, expect, test } from "vitest";

import { cartTotal, type Line } from "../total";

describe("cartTotal", () => {
  test("adds tax to the subtotal", () => {
    const items: Line[] = [
      { sku: "book", price: 10, quantity: 2 },
      { sku: "pen", price: 1.5, quantity: 4 },
    ];

    expect(cartTotal(items)).toBe(31.2);
  });
});
