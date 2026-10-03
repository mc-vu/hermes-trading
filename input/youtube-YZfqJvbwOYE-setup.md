1. Get Claude Code subscription and install claude including GIT Bash- https://code.claude.com/docs/en/setup
 2. Install VS - https://code.visualstudio.com/download
 3. Install Claude Code Extension for VS Code
 4. Clone Git Repo
 5. Setup Alpaca MCP Server in claude code. Ensure to hide keys
   i. claude mcp add --transport stdio alpaca \    --env ALPACA_API_KEY=XXXX \    --env ALPACA_SECRET_KEY=YYYY \    -- /Users/username/absolute-path-to-alpaca-mcp-server/.venv/bin/alpaca-mcp-server serve
   ii. More Safer Way:
   
   Here's a verified guide to configuring custom MCP servers safely with Claude Code locally:
   ---
   Scope Selection (Most Important Safety Decision)
   Claude Code supports MCP servers at three scope levels:
*`local` (default):* Stored in `~/.claude.json` under your project path. Private to you, only accessible in the current project directory. Best for personal dev servers, experimental configs, or servers containing *sensitive credentials**.- **`project`:* Shared with everyone via a `.mcp.json` file in your repo.- *`user`:* Available to you across all projects.
   *Safety rule of thumb:* Default to `--scope local` for anything with credentials or tokens. Never commit credentials to `.mcp.json`.
   ---
   Safest Configuration Method: CLI with `--env` Flag
   Use the `--env` flag to pass secrets rather than hardcoding them inline:
   ```bash claude mcp add my-server --scope local -e API_KEY="your-key-here" -- npx -y @your/mcp-package```
   Or reference a shell variable (never hardcode the raw value in scripts):
   ```bash export MY_API_KEY=sk-...claude mcp add my-server --scope local -e API_KEY="$MY_API_KEY" -- npx -y @your/mcp-package```
   ---
   Credential Safety Rules
   1. *Never put raw secrets in `.mcp.json`* — that file is project-scoped and may be committed to version control.2. *Use environment variable references* via `--env` or shell exports, not literal strings in config files.3. *Local scope keeps credentials private* — use it for any server with sensitive credentials that shouldn't be shared.
   ---
   Verifying Your Setup
   After adding, verify with:```bash claude mcp list        # see all configured servers claude mcp get [name]  # details for a specific server```Or inside Claude Code, run `/mcp` to check connection status.
   ---
   Direct Config File Editing (for Complex Setups)
   Directly editing the config file has advantages: complete visibility of all configurations, easy backup/copy across machines, version control tracking, and support for complex setups difficult to express through the CLI wizard.
   The config file lives at:- macOS: `~/.claude.json`- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   ---
   [Inference] Additional Security Practices
   These are logically sound but not explicitly stated in Anthropic's official docs — label accordingly:

*[Inference]* Audit tools exposed by any MCP server before enabling it, especially community/third-party ones.- *[Inference]* Prefer well-maintained, actively updated packages over obscure ones.- *[Inference]* For Alpaca or finance-related MCP servers specifically, use `local` scope exclusively and rotate API keys if they're ever accidentally exposed.
*Disclaimer:* MCP server behavior, tool permissions, and credential handling are not guaranteed by any configuration alone — they depend on the server implementation itself.
   
 6. Restart claude code in vs code
 7. Test Alpaca MCP server
 8. Start Building!