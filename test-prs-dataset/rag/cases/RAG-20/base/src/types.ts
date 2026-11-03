export interface Reading {
  meterId: string;
  /** Taken at the top of each hour. */
  at: Date;
  kwh: number;
}
