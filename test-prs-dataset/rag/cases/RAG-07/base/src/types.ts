export interface InfusionOrder {
  patientId: string;
  drug: string;
  volumeMl: number;
  durationMin: number;
}

export interface Pump {
  setRateMlPerMin(rate: number): void;
}
