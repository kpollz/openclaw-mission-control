import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ProjectOnboardingRead } from "@/api/generated/model";
import { ProjectOnboardingChat } from "./ProjectOnboardingChat";

const startOnboardingMock = vi.fn();
const getOnboardingMock = vi.fn();
const answerOnboardingMock = vi.fn();
const confirmOnboardingMock = vi.fn();

vi.mock("@/hooks/usePageActive", () => ({
  usePageActive: () => true,
}));

vi.mock("@/components/ui/dialog", () => ({
  DialogHeader: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
  DialogFooter: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
  DialogTitle: ({ children }: { children?: ReactNode }) => <h2>{children}</h2>,
}));

vi.mock("@/api/generated/project-onboarding/project-onboarding", () => ({
  startOnboardingApiV1ProjectsProjectIdOnboardingStartPost: (...args: unknown[]) =>
    startOnboardingMock(...args),
  getOnboardingApiV1ProjectsProjectIdOnboardingGet: (...args: unknown[]) =>
    getOnboardingMock(...args),
  answerOnboardingApiV1ProjectsProjectIdOnboardingAnswerPost: (
    ...args: unknown[]
  ) => answerOnboardingMock(...args),
  confirmOnboardingApiV1ProjectsProjectIdOnboardingConfirmPost: (
    ...args: unknown[]
  ) => confirmOnboardingMock(...args),
}));

const buildQuestionSession = (question: string): ProjectOnboardingRead => ({
  id: "session-1",
  project_id: "project-1",
  session_key: "session:key",
  status: "active",
  messages: [
    {
      role: "assistant",
      content: JSON.stringify({
        question,
        options: ["Option A", "Option B"],
      }),
      timestamp: "2026-02-15T00:00:00Z",
    },
  ],
  draft_goal: null,
  created_at: "2026-02-15T00:00:00Z",
  updated_at: "2026-02-15T00:00:00Z",
});

describe("ProjectOnboardingChat polling", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    startOnboardingMock.mockReset();
    getOnboardingMock.mockReset();
    answerOnboardingMock.mockReset();
    confirmOnboardingMock.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not keep polling while waiting for user answer on a shown question", async () => {
    const session = buildQuestionSession("What should we prioritize?");
    startOnboardingMock.mockResolvedValue({ status: 200, data: session });
    getOnboardingMock.mockResolvedValue({ status: 200, data: session });

    render(
      <ProjectOnboardingChat projectId="project-1" onConfirmed={() => undefined} />,
    );

    await screen.findByText("What should we prioritize?");
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Option A" })).toBeEnabled();
    });
    const callsBeforeWait = getOnboardingMock.mock.calls.length;

    await act(async () => {
      vi.advanceTimersByTime(6500);
      await Promise.resolve();
    });

    expect(getOnboardingMock.mock.calls.length).toBe(callsBeforeWait);
  });

  it("continues polling after an answer is submitted and waiting for assistant", async () => {
    const session = buildQuestionSession("Pick a style");
    startOnboardingMock.mockResolvedValue({ status: 200, data: session });
    getOnboardingMock.mockResolvedValue({ status: 200, data: session });
    answerOnboardingMock.mockResolvedValue({ status: 200, data: session });

    render(
      <ProjectOnboardingChat projectId="project-1" onConfirmed={() => undefined} />,
    );

    await screen.findByText("Pick a style");

    fireEvent.click(screen.getByRole("button", { name: "Option A" }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => {
      expect(answerOnboardingMock).toHaveBeenCalledTimes(1);
    });

    const callsBeforePoll = getOnboardingMock.mock.calls.length;
    await act(async () => {
      vi.advanceTimersByTime(2500);
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(getOnboardingMock.mock.calls.length).toBeGreaterThan(
        callsBeforePoll,
      );
    });
  });
});
