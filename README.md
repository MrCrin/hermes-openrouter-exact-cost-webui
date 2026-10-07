# hermes-openrouter-exact-cost-webui

A Hermes WebUI extension that shows the amount **OpenRouter billed** for the
current session, in the composer's context tooltip, with a per-model breakdown.

It prices each API call from OpenRouter's own generation records, rather than
from Hermes' local estimate.

Companion project: [hermes-openrouter-exact-cost](https://github.com/MrCrin/hermes-openrouter-exact-cost)
is the Hermes plugin that makes Hermes' internal per-call costs exact. Neither
project requires the other.

## What you'll see

The tooltip's cost line is replaced, not duplicated.

| Line | Meaning |
|---|---|
| `OpenRouter: $4.37` | Complete. Every call in the session was served by OpenRouter and priced from its records. |
| `OpenRouter: $4.37 (+12 calls from other providers)` | 12 calls were served by a different provider. They are not included in this figure. |
| `OpenRouter: $4.37 (+3 unpriced)` | 3 OpenRouter calls have no usable billing record. The total is a floor, not a total. |
| *(unchanged)* | The session has no OpenRouter calls, so nothing is shown. |

A per-model breakdown follows the cost line, sorted by spend:

```
deepseek-v4.1-flash    $2.2976 · 292
gemini-3.8-flash       $1.8556 · 91
gpt-6-luna-pro         $0.2156 · 10
```

Hovering the figure repeats the same detail as a sentence.

## Requirements

- Hermes WebUI with extension support, and authentication configured
- Python 3.8+ for the sidecar (standard library only, no dependencies)
- `OPENROUTER_API_KEY` in `$HERMES_HOME/.env` or the environment

## Install

```bash
EXT=hermes-openrouter-exact-cost
DST="$HOME/.hermes/webui/extensions/$EXT"

# Files
mkdir -p "$DST"
cp -r extension.json manifest.json assets sidecar "$DST/"
find "$DST" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# Sidecar
mkdir -p "$HOME/.config/systemd/user"
cp hermes-openrouter-exact-cost-sidecar.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now "$EXT-sidecar"

# Register in the WebUI's install manifest
python3 register_extension.py "$DST"
```

The WebUI does not scan the extensions directory: its **Installed** list is built
from `extension-install-manifest.json`, which `register_extension.py` writes. An
extension copied in by hand will not appear until it has an entry there.

Then, in the WebUI:

1. **Settings → Extensions → Diagnostics → Loopback sidecars**
2. Click **Approve proxy consent** next to OpenRouter Exact Cost
3. Hard-reload the page (**Ctrl+Shift+R**)

Step 2 is required. Granting consent mints the sidecar's proxy token; until it
exists, the extension's requests to its own sidecar are refused.

## Configure

None. The sidecar reads `OPENROUTER_API_KEY` from `$HERMES_HOME/.env`.

If your Hermes home is not `~/.hermes`, set `HERMES_HOME` in the unit's
`Environment=` line and adjust `WorkingDirectory` accordingly.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Not listed in Settings → Extensions | No entry in `extension-install-manifest.json`. Run `register_extension.py`. |
| Listed and enabled, but the tooltip is unchanged | Proxy consent not granted, so no token was minted. |
| Tooltip flickers between two values | Cached script. Hard-reload. |

Sidecar log: `journalctl --user -u hermes-openrouter-exact-cost-sidecar -n 50`

## Limits

- OpenRouter only. Calls served by another provider cannot be priced from
  OpenRouter's records; they are counted and disclosed, never estimated.
- The total excludes non-OpenRouter spend, including when it is disclosed.
- Generation ids are read from Hermes' agent log. A call whose log line is
  rotated away before the sidecar reads it cannot be recovered.
- A call OpenRouter has just billed may not be queryable for a short period.
  Such calls are counted as pending and resolve themselves.
- If the sidecar is not running, the tooltip reverts to the WebUI's own figure
  with no indication that it is unverified.

## Uninstall

```bash
systemctl --user disable --now hermes-openrouter-exact-cost-sidecar
rm -f  ~/.config/systemd/user/hermes-openrouter-exact-cost-sidecar.service
rm -rf ~/.hermes/webui/extensions/hermes-openrouter-exact-cost
rm -f  ~/.hermes/webui/sidecar-auth/hermes-openrouter-exact-cost.token
```

Then remove its entry from `extension-install-manifest.json`, or use the WebUI's
own Uninstall button.

## Licence

MIT — see [LICENSE](LICENSE).
