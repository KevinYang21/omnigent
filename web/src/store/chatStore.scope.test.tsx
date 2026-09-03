import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { conversationRegistry } from "./conversationRegistry";
import { ChatStoreScopeProvider, bindConversationForTest, useChatStore } from "./chatStore";

function StatusProbe({ label }: { label: string }) {
  const status = useChatStore((state) => state.status);
  return <div data-testid={label}>{status}</div>;
}

describe("ChatStoreScopeProvider", () => {
  beforeEach(() => {
    conversationRegistry.clear();
    bindConversationForTest(null);
  });

  it("selects and updates each conversation entry independently", () => {
    bindConversationForTest("session-a", { status: "streaming" });
    const sessionB = conversationRegistry.acquire("session-b");
    sessionB.setState({ status: "idle" });

    render(
      <>
        <ChatStoreScopeProvider conversationId="session-a">
          <StatusProbe label="session-a" />
        </ChatStoreScopeProvider>
        <ChatStoreScopeProvider conversationId="session-b">
          <StatusProbe label="session-b" />
        </ChatStoreScopeProvider>
      </>,
    );

    expect(screen.getByTestId("session-a")).toHaveTextContent("streaming");
    expect(screen.getByTestId("session-b")).toHaveTextContent("idle");

    act(() => sessionB.setState({ status: "streaming" }));

    expect(screen.getByTestId("session-a")).toHaveTextContent("streaming");
    expect(screen.getByTestId("session-b")).toHaveTextContent("streaming");
    expect(useChatStore.getState().conversationId).toBe("session-a");
  });
});
