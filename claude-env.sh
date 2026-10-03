# Source this before launching Claude Code against a local halogen server:
#   source ./claude-env.sh && claude

export ANTHROPIC_BASE_URL="http://127.0.0.1:8731"
export ANTHROPIC_AUTH_TOKEN="local"
unset ANTHROPIC_API_KEY

# Claude Code otherwise assumes a Sonnet-sized window and compacts far too late,
# or not at all, until a turn dies with "max_tokens N does not fit".
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=250000
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=220000

# Thinking and the answer share this budget; 16384 leaves room for both and
# keeps prompt + output inside the 262,144 context.
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=16384

# A cold 33k prompt takes ~23 s to prefill; the default client timeout hangs up
# before the first token.
export API_TIMEOUT_MS=1800000

export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1