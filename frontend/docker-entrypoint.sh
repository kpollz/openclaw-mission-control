#!/bin/sh
set -e

# Generate runtime configuration readable by the browser.
# This avoids rebuilding the Docker image when only ports change.
# Next.js serves everything under public/ as static assets.
mkdir -p /app/public
cat > /app/public/runtime-config.js <<EOF
window.__RUNTIME_CONFIG__ = { backendPort: "${BACKEND_PORT:-8000}" };
EOF

exec "$@"
