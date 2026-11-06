export type Invite = {
  id: string;
  sender: string;
  /** Free text typed by the person who sent the invite. */
  note: string;
  sentAt: string;
};
