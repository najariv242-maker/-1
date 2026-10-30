#!/usr/bin/env bash
set -u

if [ -z "${BOT_TOKEN:-}" ] || [ -z "${GEMINI_API_KEY:-}" ]; then
    echo "BOT_TOKEN and GEMINI_API_KEY must be configured."
    exit 1
fi

echo "===================================================="
echo "🚀 Starting 24/7 Live Hot-Reload & Watchdog Runner"
echo "🤖 Bot: @voiceandpasokhbot (Public-Safe Edition)"
echo "===================================================="

# زمان اجرای هر سیکل (حدود 340 دقیقه، قبل از تایم‌اوت 350 دقیقه‌ای گیت‌هاب اکشنز)
MAX_RUNTIME_SECONDS=$((340 * 60))
START_TIME=$(date +%s)

BOT_PID=""
BRANCH_NAME="${GITHUB_REF_NAME:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'main')}"
if [ "$BRANCH_NAME" = "HEAD" ] || [ -z "$BRANCH_NAME" ]; then
    BRANCH_NAME="main"
fi
LAST_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo "initial")
POLL_COUNTER=0

start_bot() {
    echo "▶️ [$(date '+%Y-%m-%d %H:%M:%S')] Launching bot.py..."
    python3 bot.py &
    BOT_PID=$!
    echo "✅ Bot started with PID: $BOT_PID"
}

stop_bot() {
    if [ -n "$BOT_PID" ] && kill -0 "$BOT_PID" 2>/dev/null; then
        echo "⏹️ Stopping bot (PID: $BOT_PID)..."
        kill -TERM "$BOT_PID" 2>/dev/null || true
        for _ in 1 2 3 4; do
            if ! kill -0 "$BOT_PID" 2>/dev/null; then
                break
            fi
            sleep 1
        done
        if kill -0 "$BOT_PID" 2>/dev/null; then
            kill -9 "$BOT_PID" 2>/dev/null || true
        fi
        wait "$BOT_PID" 2>/dev/null || true
    fi
}

trap 'stop_bot; exit 0' SIGINT SIGTERM

start_bot

# حلقه ترکیبی Watchdog (بررسی هر ۵ ثانیه) و Hot-Reload (بررسی آپدیت گیت‌هاب هر ۶۰ ثانیه)
while true; do
    CURRENT_TIME=$(date +%s)
    ELAPSED=$((CURRENT_TIME - START_TIME))

    if [ "$ELAPSED" -ge "$MAX_RUNTIME_SECONDS" ]; then
        echo "⏰ Cycle time limit reached ($ELAPSED seconds). Handing over to next workflow cycle..."
        stop_bot
        break
    fi

    # 1. بررسی زنده بودن بات (Watchdog)
    if ! kill -0 "$BOT_PID" 2>/dev/null; then
        echo "⚠️ [Watchdog] bot.py exited! Restarting in 3 seconds..."
        sleep 3
        start_bot
    fi

    # 2. بررسی تغییرات مخزن گیت‌هاب هر 60 ثانیه (12 سیکل 5 ثانیه‌ای) و جلوگیری از حلقه بی‌نهایت
    POLL_COUNTER=$((POLL_COUNTER + 1))
    if [ $((POLL_COUNTER % 12)) -eq 0 ]; then
        if git fetch origin "$BRANCH_NAME" --quiet 2>/dev/null; then
            REMOTE_COMMIT=$(git rev-parse "origin/$BRANCH_NAME" 2>/dev/null || echo "$LAST_COMMIT")
            if [ -n "$REMOTE_COMMIT" ] && [ "$REMOTE_COMMIT" != "$LAST_COMMIT" ]; then
                echo "🔄 [Hot-Reload] New commit detected ($REMOTE_COMMIT)! Updating live..."
                if git merge --ff-only "origin/$BRANCH_NAME" 2>/dev/null; then
                    if [ -f requirements.txt ]; then
                        python3 -m pip install -q -r requirements.txt || true
                    fi
                    LAST_COMMIT="$REMOTE_COMMIT"
                    stop_bot
                    start_bot
                else
                    echo "⚠️ Cannot fast-forward; preserving local files and retrying later."
                fi
            fi
        fi
    fi

    sleep 5
done

echo "✅ Cycle finished cleanly."
