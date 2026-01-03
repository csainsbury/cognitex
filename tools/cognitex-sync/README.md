# cognitex-sync

Sync coding CLI sessions (Claude Code, etc.) to your Cognitex knowledge graph.

## Installation

```bash
# From PyPI (when published)
pip install cognitex-sync

# From source
cd tools/cognitex-sync
pip install -e .
```

## Quick Start

### 1. Configure

```bash
# Set your Cognitex server and API key
cognitex-sync configure --server https://your-cognitex-server.com --api-key YOUR_API_KEY

# Optionally set a custom machine ID
cognitex-sync configure --machine-id my-laptop
```

### 2. Check Status

```bash
cognitex-sync status
```

### 3. Sync Sessions

```bash
# Push all new sessions
cognitex-sync push

# Force re-sync all sessions
cognitex-sync push --force

# Dry run (see what would be synced)
cognitex-sync push --dry-run
```

### 4. Run as Daemon (Optional)

```bash
# Sync every 30 minutes in the background
cognitex-sync daemon --interval 30
```

## Commands

| Command | Description |
|---------|-------------|
| `configure` | Set server URL, API key, and machine ID |
| `status` | Check connection and show sync info |
| `push` | Push coding sessions to server |
| `push-file` | Push a single session file (for hooks) |
| `daemon` | Run as background sync daemon |
| `discover` | List local coding sessions |
| `hook-install` | Install Claude Code hook for auto-sync |

## Configuration

Configuration is stored in `~/.config/cognitex-sync/config.json`.

You can also use environment variables:
- `COGNITEX_SYNC_SERVER_URL` - Server URL
- `COGNITEX_SYNC_API_KEY` - API key
- `COGNITEX_SYNC_MACHINE_ID` - Machine identifier

## How It Works

1. **Discovery**: Scans `~/.claude/projects/` for session JSONL files
2. **Parsing**: Extracts messages, timestamps, and metadata from sessions
3. **Sync**: Sends session data to Cognitex API for processing
4. **LLM Extraction**: Server uses LLM to extract summaries, decisions, and next steps
5. **Graph Storage**: Sessions are stored in Neo4j, linked to projects and repositories

## Supported CLI Tools

- [x] Claude Code (`~/.claude/projects/`)
- [ ] Codex CLI (planned)
- [ ] Gemini CLI (planned)

## Server Setup

On your Cognitex server, ensure:

1. `SYNC_API_KEY` is set in the environment
2. Web server is accessible from your local machine
3. Neo4j is running and configured

Generate an API key:
```bash
# On the server
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add to your `.env`:
```
SYNC_API_KEY=your-generated-key
```

## License

MIT
