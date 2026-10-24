/**
 * The command that connects Claude Code to FishSense with a read token (0066).
 * Pure, so the tokens page's client form builds it from the real endpoint and
 * token, with no placeholder to swap.
 */
export function mcpCommand(url: string, token: string): string {
  return `claude mcp add --transport http fishsense ${url} --header "Authorization: Bearer ${token}"`;
}
