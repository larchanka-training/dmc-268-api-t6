import { useReducer, type Dispatch } from "react";

export type ReviewRun = {
  runId: string;
  phase: "queued" | "running" | "cancelled" | "passed" | "failed";
};

export type ReviewRunAction = {
  type: "started" | "cancelled" | "completed" | "failed";
  runId: string;
};

export function transitionReviewRun(
  state: ReviewRun,
  action: ReviewRunAction,
): ReviewRun {
  if (action.runId !== state.runId) {
    return state;
  }

  if (action.type === "started" && state.phase === "queued") {
    return { ...state, phase: "running" };
  }

  if (
    action.type === "cancelled" &&
    (state.phase === "queued" || state.phase === "running")
  ) {
    return { ...state, phase: "cancelled" };
  }

  if (action.type === "completed" && state.phase === "running") {
    return { ...state, phase: "passed" };
  }

  if (action.type === "failed" && state.phase === "running") {
    return { ...state, phase: "failed" };
  }

  return state;
}

export function useReviewRun(
  initial: ReviewRun,
): [ReviewRun, Dispatch<ReviewRunAction>] {
  return useReducer(transitionReviewRun, initial);
}
