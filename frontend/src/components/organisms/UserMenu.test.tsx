import type {
  AnchorHTMLAttributes,
  ImgHTMLAttributes,
  PropsWithChildren,
} from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { UserMenu } from "./UserMenu";

const useUserMock = vi.hoisted(() => vi.fn());
const clearPasswordTokensMock = vi.hoisted(() => vi.fn());
type LinkProps = PropsWithChildren<{
  href: string | { pathname?: string };
}> &
  Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href">;

vi.mock("next/image", () => ({
  default: (props: ImgHTMLAttributes<HTMLImageElement>) => (
    // eslint-disable-next-line @next/next/no-img-element
    <img {...props} alt={props.alt ?? ""} />
  ),
}));

vi.mock("next/link", () => ({
  default: ({ children, href, ...rest }: LinkProps) => (
    <a href={typeof href === "string" ? href : "#"} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock("@/auth/clerk", () => ({
  useUser: useUserMock,
}));

vi.mock("@/auth/passwordAuth", () => ({
  clearPasswordTokens: clearPasswordTokensMock,
}));

describe("UserMenu", () => {
  beforeEach(() => {
    useUserMock.mockReset();
    clearPasswordTokensMock.mockReset();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders and opens menu actions", async () => {
    const user = userEvent.setup();
    useUserMock.mockReturnValue({ isSignedIn: true, user: null });

    render(<UserMenu />);

    await user.click(screen.getByRole("button", { name: /open user menu/i }));

    expect(
      screen.getByRole("link", { name: /open projects/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /create project/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /sign out/i }),
    ).toBeInTheDocument();
  });

  it("clears password tokens and reloads on sign out", async () => {
    const user = userEvent.setup();
    useUserMock.mockReturnValue({ isSignedIn: true, user: null });
    const reloadSpy = vi.fn();
    vi.stubGlobal("location", {
      ...window.location,
      reload: reloadSpy,
    } as Location);

    render(<UserMenu />);

    await user.click(screen.getByRole("button", { name: /open user menu/i }));
    await user.click(screen.getByRole("button", { name: /sign out/i }));

    expect(clearPasswordTokensMock).toHaveBeenCalledTimes(1);
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });
});
