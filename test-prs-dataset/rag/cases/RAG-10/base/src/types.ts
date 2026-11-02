export interface Reading {
  id: string;
  celsius: number;
}

export interface Sensor {
  raw(): number;
}
