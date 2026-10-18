# glance-plugin-usage

Percent used across AI tools — Cursor, Grok, OpenCode Go, Antigravity,
Devin — for the [glance](https://github.com/neatstudio/glance) hub.

Entities: `usage.rows` (table + `lines` + `names`) and one per tool
(`usage.cursor`, `usage.grok`, `usage.ocgo`, `usage.agy`, `usage.devin`)
with `pct_5h`, `pct_week`, `line`.

## Credentials

Nothing is read from the environment. Two kinds of input:

1. **Files** from the machine where the tools are logged in, mounted
   read-only into the plugin's data directory
   `data/plugins/usage/data/secrets/`:

   | file                                          | from                                                                    |
   | --------------------------------------------- | ----------------------------------------------------------------------- |
   | `cursor-state.vscdb`                          | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`   |
   | `grok/auth.json`                              | `~/.grok/auth.json` (mount the folder read-write: tokens get refreshed) |
   | `opencode-auth.json`, `opencode.json`         | `~/.local/share/opencode/auth.json`, `~/.config/opencode/opencode.json` |
   | `agy-accounts.json`                           | `~/.config/opencode/antigravity-accounts.json`                          |
   | `devin-state.vscdb`, `devin-credentials.toml` | Devin app state and `~/.local/share/devin/credentials.toml`             |

2. **Keys** in the hub secret store (插件 → 密钥, or `PUT /api/hub/secrets/<name>`):
   `xai`, `opencode`, `opencode_go`, `devin`, `devin_seat_url`,
   `agy_client_id`, `agy_client_secret`. Missing ones just leave that
   row empty.

Install: `github:neatstudio/glance-plugin-usage@v1.0.0`, accept the
permissions (`network`, `filesystem:data`, `secrets:*` above).
