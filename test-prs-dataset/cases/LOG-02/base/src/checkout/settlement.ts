export type PaymentStatus = "captured" | "pending" | "declined";

export interface GatewayResult {
  orderId: string;
  status: PaymentStatus;
}

export interface FulfillmentDecision {
  orderId: string;
  paymentState: "paid" | "awaiting_payment" | "payment_failed";
  releaseShipment: boolean;
}

export function decideFulfillment(result: GatewayResult): FulfillmentDecision {
  if (result.status === "captured") {
    return {
      orderId: result.orderId,
      paymentState: "paid",
      releaseShipment: true,
    };
  }

  if (result.status === "pending") {
    return {
      orderId: result.orderId,
      paymentState: "awaiting_payment",
      releaseShipment: false,
    };
  }

  return {
    orderId: result.orderId,
    paymentState: "payment_failed",
    releaseShipment: false,
  };
}
