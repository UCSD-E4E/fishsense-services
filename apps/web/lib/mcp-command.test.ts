// The command that connects Claude Code to FishSense's MCP endpoint, built
// from the real URL and token (the PR #335 review: a placeholder swapped in
// afterwards broke on a URL that held the placeholder's text).
import { describe, expect, it } from "vitest";
import { mcpCommand } from "./mcp-command";

describe("mcpCommand", () => {
  it("names the endpoint and sends the token as a Bearer header", () => {
    expect(mcpCommand("https://api.fishsense.e4e.ucsd.edu/mcp", "fishsense_abc")).toBe(
      'claude mcp add --transport http fishsense https://api.fishsense.e4e.ucsd.edu/mcp --header "Authorization: Bearer fishsense_abc"',
    );
  });

  it("leaves a URL that looks like a placeholder alone", () => {
    expect(mcpCommand("https://TOKEN-gw.example/mcp", "fishsense_abc")).toBe(
      'claude mcp add --transport http fishsense https://TOKEN-gw.example/mcp --header "Authorization: Bearer fishsense_abc"',
    );
  });
});
