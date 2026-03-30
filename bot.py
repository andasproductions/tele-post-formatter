import asyncio
import functools
import json
import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)
from names import extract_names
from lookup import lookup_all, twitter_search_url, instagram_search_url
from formatter import apply_substitutions, format_platform

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_data_dir = os.environ.get("DATA_DIR", os.path.dirname(__file__))
CONFIG_PATH = os.path.join(_data_dir, "config.json")

_SAMPLE_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.sample.json")

ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])

# ── Conversation states ────────────────────────────────────────────────────────
(
    CONFIRM_NAMES,       # 0 — main conv
    AWAIT_MANUAL_NAMES,  # 1 — main conv
    AWAIT_HANDLE_INPUT,  # 2 — main conv
    SETUP_PLATFORMS,     # 3 — setup conv
    SETUP_FIELD,         # 4 — setup conv
    SETUP_CONFIRM,       # 5 — setup conv
    MANAGE_USERS,        # 6 — users conv
    ADD_USER,            # 7 — users conv
    DELETE_USER,         # 8 — users conv
) = range(9)


# ── Config helpers ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        with open(_SAMPLE_CONFIG_PATH) as f:
            defaults = json.load(f)
        save_config(defaults)
        return defaults
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ── Auth ───────────────────────────────────────────────────────────────────────

def is_authorized(user_id: int) -> bool:
    if user_id == ADMIN_USER_ID:
        return True
    return user_id in load_config().get("allowed_users", [])


def admin_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_USER_ID:
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


def authorized_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update.effective_user.id):
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


# ── Output sender ──────────────────────────────────────────────────────────────

PLATFORM_EMOJI = {
    "instagram": "📸 INSTAGRAM",
    "twitter":   "🐦 TWITTER",
    "bluesky":   "🦋 BLUESKY",
}


async def send_formatted_output(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = context.user_data["original_text"]
    substitutions = context.user_data.get("substitutions", {})
    config = load_config()

    platform_texts = apply_substitutions(text, substitutions)

    for platform in ["instagram", "twitter", "bluesky"]:
        if not config.get(platform, {}).get("enabled", True):
            continue
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=PLATFORM_EMOJI[platform],
        )
        chunks = format_platform(platform_texts[platform], platform, config)
        for chunk in chunks:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=chunk,
            )


# ── Name confirmation ──────────────────────────────────────────────────────────

def build_name_message(lookup: dict) -> str:
    name = lookup["name"]
    lines = [f"🔍 *{name}*\n"]

    lines.append("🦋 *Bluesky* — pick one:")
    if lookup["bluesky"]:
        for i, actor in enumerate(lookup["bluesky"]):
            lines.append(f"  {i+1}\\. [{actor['handle']}]({actor['url']}) · {actor['display_name']}")
    else:
        lines.append("  _No results found_")

    lines.append("\n🐦 *Twitter*")
    if lookup["twitter"]:
        lines.append(f"  {lookup['twitter']}")
    else:
        lines.append("  _No results found_")

    lines.append("\n📸 *Instagram*")
    if lookup["instagram"]:
        lines.append(f"  {lookup['instagram']}")
    else:
        lines.append("  _No results found_")

    return "\n".join(lines)


def build_name_keyboard(lookup: dict, name_idx: int) -> InlineKeyboardMarkup:
    rows = []

    # Bluesky: one button per result + skip
    for i, actor in enumerate(lookup["bluesky"]):
        rows.append([InlineKeyboardButton(
            f"{i+1}. @{actor['handle']} · {actor['display_name']}",
            callback_data=f"bsky:{name_idx}:{i}"
        )])
    rows.append([InlineKeyboardButton("🦋 Skip Bluesky", callback_data=f"bsky:{name_idx}:skip")])

    # Twitter
    tw_row = []
    if lookup["twitter"]:
        tw_row.append(InlineKeyboardButton("🐦 ✅ Use", callback_data=f"tw:{name_idx}:use"))
        tw_row.append(InlineKeyboardButton("✏️ Correct", callback_data=f"tw:{name_idx}:edit"))
    else:
        tw_row.append(InlineKeyboardButton("🐦 ✏️ Add handle", callback_data=f"tw:{name_idx}:edit"))
    tw_row.append(InlineKeyboardButton("Skip", callback_data=f"tw:{name_idx}:skip"))
    rows.append(tw_row)

    # Instagram
    ig_row = []
    if lookup["instagram"]:
        ig_row.append(InlineKeyboardButton("📸 ✅ Use", callback_data=f"ig:{name_idx}:use"))
        ig_row.append(InlineKeyboardButton("✏️ Correct", callback_data=f"ig:{name_idx}:edit"))
    else:
        ig_row.append(InlineKeyboardButton("📸 ✏️ Add handle", callback_data=f"ig:{name_idx}:edit"))
    ig_row.append(InlineKeyboardButton("Skip", callback_data=f"ig:{name_idx}:skip"))
    rows.append(ig_row)

    return InlineKeyboardMarkup(rows)


def is_resolved(entry: dict) -> bool:
    return all(entry.get(k, "pending") != "pending" for k in ("bluesky", "twitter", "instagram"))


async def show_next_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lookups = context.user_data["lookups"]
    idx = context.user_data.get("current_name_idx", 0)

    if idx >= len(lookups):
        await send_formatted_output(update, context)
        return ConversationHandler.END

    lookup = lookups[idx]
    context.user_data.setdefault("resolved", {})[idx] = {
        "bluesky": "pending",
        "twitter": "pending",
        "instagram": "pending",
    }

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=build_name_message(lookup),
        parse_mode="Markdown",
        reply_markup=build_name_keyboard(lookup, idx),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    return CONFIRM_NAMES


async def try_advance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data["current_name_idx"]
    resolved = context.user_data["resolved"]

    if is_resolved(resolved[idx]):
        lookups = context.user_data["lookups"]
        name = lookups[idx]["name"]
        r = resolved[idx]
        subs = context.user_data.setdefault("substitutions", {})
        subs[name] = {
            "twitter":   r["twitter"]   if r["twitter"]   != "skip" else None,
            "bluesky":   r["bluesky"]   if r["bluesky"]   != "skip" else None,
            "instagram": r["instagram"] if r["instagram"] != "skip" else None,
        }
        context.user_data["current_name_idx"] = idx + 1
        return await show_next_name(update, context)

    return CONFIRM_NAMES


# ── Callback handler ───────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # No-names prompt
    if data == "detect:yes":
        await query.edit_message_text("Type the names you want to look up, one per line:")
        return AWAIT_MANUAL_NAMES

    if data == "detect:no":
        await query.edit_message_text("Formatting…")
        await send_formatted_output(update, context)
        return ConversationHandler.END

    # Name resolution callbacks
    parts = data.split(":")
    platform_code, name_idx, action = parts[0], int(parts[1]), parts[2]
    lookups = context.user_data["lookups"]
    lookup = lookups[name_idx]
    resolved = context.user_data["resolved"][name_idx]

    if platform_code == "bsky":
        if action == "skip":
            resolved["bluesky"] = "skip"
        else:
            actor = lookup["bluesky"][int(action)]
            resolved["bluesky"] = f"@{actor['handle']}"

    elif platform_code == "tw":
        if action == "skip":
            resolved["twitter"] = "skip"
        elif action == "use":
            url = lookup["twitter"]
            handle = "@" + url.rstrip("/").split("/")[-1]
            resolved["twitter"] = handle
        elif action == "edit":
            context.user_data["editing_handle"] = {"name_idx": name_idx, "platform": "twitter"}
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔍 Search on X", url=twitter_search_url(lookup["name"]))
            ]])
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Type the correct Twitter handle (e.g. @handle), or search first:",
                reply_markup=keyboard,
            )
            return AWAIT_HANDLE_INPUT

    elif platform_code == "ig":
        if action == "skip":
            resolved["instagram"] = "skip"
        elif action == "use":
            url = lookup["instagram"]
            handle = "@" + url.rstrip("/").split("/")[-1]
            resolved["instagram"] = handle
        elif action == "edit":
            context.user_data["editing_handle"] = {"name_idx": name_idx, "platform": "instagram"}
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔍 Search on Instagram", url=instagram_search_url(lookup["name"]))
            ]])
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Type the correct Instagram handle (e.g. @handle), or search first:",
                reply_markup=keyboard,
            )
            return AWAIT_HANDLE_INPUT

    return await try_advance(update, context)


# ── Handle input ───────────────────────────────────────────────────────────────

async def receive_handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lstrip("@")
    handle = f"@{raw}"
    editing = context.user_data["editing_handle"]
    name_idx = editing["name_idx"]
    platform = editing["platform"]

    context.user_data["resolved"][name_idx][platform] = handle
    await update.message.reply_text(f"✅ Set to {handle}")
    return await try_advance(update, context)


# ── Manual name entry ──────────────────────────────────────────────────────────

async def receive_manual_names(update: Update, context: ContextTypes.DEFAULT_TYPE):
    names = [n.strip() for n in update.message.text.strip().splitlines() if n.strip()]
    if not names:
        await update.message.reply_text("No names found. Try again or /cancel.")
        return AWAIT_MANUAL_NAMES

    await update.message.reply_text(f"Looking up {len(names)} name(s)…")
    lookups = await asyncio.gather(*[lookup_all(n) for n in names])
    context.user_data["lookups"] = list(lookups)
    context.user_data["current_name_idx"] = 0
    return await show_next_name(update, context)


# ── Main text receiver ─────────────────────────────────────────────────────────

@authorized_only
async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = (message.text or message.caption or "").strip()

    if not text:
        await message.reply_text("Send me some text to format.")
        return ConversationHandler.END

    context.user_data.update({
        "original_text": text,
        "substitutions": {},
        "current_name_idx": 0,
        "lookups": [],
        "resolved": {},
    })

    config = load_config()
    names = extract_names(text, config.get("ignored_names", []))

    if not names:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, add names", callback_data="detect:yes"),
            InlineKeyboardButton("No, just format", callback_data="detect:no"),
        ]])
        await message.reply_text(
            "No names detected. Want to add handles manually?",
            reply_markup=keyboard,
        )
        return CONFIRM_NAMES

    await message.reply_text(f"Found: {', '.join(names)}. Looking up handles…")
    lookups = await asyncio.gather(*[lookup_all(n) for n in names])
    context.user_data["lookups"] = list(lookups)
    return await show_next_name(update, context)


# ── Setup wizard (/start and /config) ─────────────────────────────────────────

PLATFORM_SETUP = [
    ("twitter",   "🐦 Twitter"),
    ("bluesky",   "🦋 Bluesky"),
    ("instagram", "📸 Instagram"),
]


def _build_platforms_keyboard(enabled_map: dict) -> InlineKeyboardMarkup:
    rows = []
    for platform, label in PLATFORM_SETUP:
        mark = "✅" if enabled_map[platform] else "❌"
        rows.append([InlineKeyboardButton(
            f"{mark} {label}",
            callback_data=f"setup:toggle:{platform}",
        )])
    any_enabled = any(enabled_map.values())
    if any_enabled:
        rows.append([InlineKeyboardButton("Next →", callback_data="setup:next")])
    return InlineKeyboardMarkup(rows)


def _build_steps_list(enabled_map: dict) -> list:
    steps = []
    for platform, _ in PLATFORM_SETUP:
        if enabled_map[platform]:
            steps.append((platform, "prefix"))
            steps.append((platform, "suffix"))
    steps.append(("ignored_names", "ignored_names"))
    return steps


def _seed_setup_data(context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    context.user_data["setup_platform_enabled"] = {
        p: cfg.get(p, {}).get("enabled", True)
        for p, _ in PLATFORM_SETUP
    }
    context.user_data["setup_steps"] = []
    context.user_data["setup_index"] = 0
    context.user_data["setup_pending"] = {}


async def _show_platforms_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    enabled_map = context.user_data["setup_platform_enabled"]
    keyboard = _build_platforms_keyboard(enabled_map)
    await update.message.reply_text(
        "Which platforms are you posting to? Tap to toggle, then press Next.",
        reply_markup=keyboard,
    )
    return SETUP_PLATFORMS


@authorized_only
async def setup_start_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _seed_setup_data(context)
    await update.message.reply_text(
        "Welcome! Let's set up your formatter.\n\n"
        "You can run /start again at any time to update these settings."
    )
    return await _show_platforms_step(update, context)


@authorized_only
async def setup_config_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _seed_setup_data(context)
    await update.message.reply_text("Updating your config.")
    return await _show_platforms_step(update, context)


async def setup_platforms_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("setup:toggle:"):
        platform = data.split(":", 2)[2]
        enabled_map = context.user_data["setup_platform_enabled"]
        enabled_map[platform] = not enabled_map[platform]
        await query.edit_message_reply_markup(
            reply_markup=_build_platforms_keyboard(enabled_map)
        )
        return SETUP_PLATFORMS

    if data == "setup:next":
        enabled_map = context.user_data["setup_platform_enabled"]
        context.user_data["setup_steps"] = _build_steps_list(enabled_map)
        context.user_data["setup_index"] = 0
        context.user_data["setup_pending"] = {}
        await query.edit_message_reply_markup(reply_markup=None)
        return await _show_field_step(update, context)


def _field_label(platform: str, field: str) -> str:
    if platform == "ignored_names":
        return "Ignored names (comma-separated)"
    labels = {"twitter": "🐦 Twitter", "bluesky": "🦋 Bluesky", "instagram": "📸 Instagram"}
    return f"{labels[platform]} {field}"


def _current_field_value(platform: str, field: str) -> str:
    cfg = load_config()
    if platform == "ignored_names":
        return ", ".join(cfg.get("ignored_names", []))
    return cfg.get(platform, {}).get(field, "")


async def _show_field_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    steps = context.user_data["setup_steps"]
    idx = context.user_data["setup_index"]

    if idx >= len(steps):
        return await _show_confirm_step(update, context)

    platform, field = steps[idx]
    label = _field_label(platform, field)
    current = _current_field_value(platform, field)
    display = current if current else "(empty)"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Keep", callback_data="setup:keep"),
    ]])
    text = f"*{label}*\nCurrent: `{display}`\n\nType a new value, or press Keep."

    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

    return SETUP_FIELD


async def setup_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    steps = context.user_data["setup_steps"]
    idx = context.user_data["setup_index"]
    platform, field = steps[idx]

    # Keep current value
    current = _current_field_value(platform, field)
    if platform == "ignored_names":
        context.user_data["setup_pending"][("ignored_names", "ignored_names")] = [
            n.strip() for n in current.split(",") if n.strip()
        ]
    else:
        context.user_data["setup_pending"][(platform, field)] = current

    context.user_data["setup_index"] = idx + 1
    return await _show_field_step(update, context)


async def setup_field_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    steps = context.user_data["setup_steps"]
    idx = context.user_data["setup_index"]
    platform, field = steps[idx]
    value = update.message.text.strip()

    if platform == "ignored_names":
        context.user_data["setup_pending"][("ignored_names", "ignored_names")] = [
            n.strip() for n in value.split(",") if n.strip()
        ]
    else:
        context.user_data["setup_pending"][(platform, field)] = value

    context.user_data["setup_index"] = idx + 1
    return await _show_field_step(update, context)


def _build_confirm_message(enabled_map: dict, pending: dict) -> str:
    lines = ["*Ready to save:*\n"]
    for platform, label in PLATFORM_SETUP:
        enabled = enabled_map[platform]
        mark = "✅" if enabled else "❌"
        lines.append(f"{label}: {mark}")
        if enabled:
            prefix = pending.get((platform, "prefix"), _current_field_value(platform, "prefix"))
            suffix = pending.get((platform, "suffix"), _current_field_value(platform, "suffix"))
            lines.append(f"  Prefix: `{prefix or '(empty)'}`")
            lines.append(f"  Suffix: `{suffix or '(empty)'}`")
    ignored = pending.get(
        ("ignored_names", "ignored_names"),
        load_config().get("ignored_names", [])
    )
    lines.append(f"\n🚫 Ignored names: `{', '.join(ignored) or '(none)'}`")
    return "\n".join(lines)


async def _show_confirm_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    enabled_map = context.user_data["setup_platform_enabled"]
    pending = context.user_data["setup_pending"]
    text = _build_confirm_message(enabled_map, pending)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💾 Save", callback_data="setup:save"),
    ]])

    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

    return SETUP_CONFIRM


async def setup_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cfg = load_config()
    enabled_map = context.user_data["setup_platform_enabled"]
    pending = context.user_data["setup_pending"]

    for platform, _ in PLATFORM_SETUP:
        cfg.setdefault(platform, {})["enabled"] = enabled_map[platform]

    for (platform, field), value in pending.items():
        if platform == "ignored_names":
            cfg["ignored_names"] = value
        else:
            cfg.setdefault(platform, {})[field] = value

    save_config(cfg)
    await query.edit_message_text("✅ Config saved! Send me any text to format.")
    return ConversationHandler.END


# ── /users command (admin only) ───────────────────────────────────────────────

def build_users_message(allowed: list[int]) -> str:
    if not allowed:
        return "👥 *Allowed users*\n\n_(none)_"
    lines = ["👥 *Allowed users*\n"]
    for uid in allowed:
        lines.append(f"• `{uid}`")
    return "\n".join(lines)


def build_users_keyboard(allowed: list[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ Add user", callback_data="usr:add")]]
    if allowed:
        rows.append([InlineKeyboardButton("🗑 Delete user", callback_data="usr:delete")])
    return InlineKeyboardMarkup(rows)


def build_delete_keyboard(allowed: list[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(str(uid), callback_data=f"usr:remove:{uid}")] for uid in allowed]
    return InlineKeyboardMarkup(rows)


@admin_only
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    allowed = cfg.get("allowed_users", [])
    await update.message.reply_text(
        build_users_message(allowed),
        parse_mode="Markdown",
        reply_markup=build_users_keyboard(allowed),
    )
    return MANAGE_USERS


async def handle_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cfg = load_config()
    allowed = cfg.get("allowed_users", [])

    if query.data == "usr:add":
        await query.edit_message_text("Send the Telegram user ID to add:")
        return ADD_USER

    if query.data == "usr:delete":
        await query.edit_message_text(
            "Select a user to remove:",
            reply_markup=build_delete_keyboard(allowed),
        )
        return DELETE_USER

    if query.data.startswith("usr:remove:"):
        uid = int(query.data.split(":")[-1])
        allowed = [u for u in allowed if u != uid]
        cfg["allowed_users"] = allowed
        save_config(cfg)

    await query.edit_message_text(
        build_users_message(allowed),
        parse_mode="Markdown",
        reply_markup=build_users_keyboard(allowed),
    )
    return MANAGE_USERS


async def receive_new_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    try:
        uid = int(raw)
    except ValueError:
        await update.message.reply_text("That doesn't look like a valid user ID. Try again or /cancel.")
        return ADD_USER
    cfg = load_config()
    allowed = cfg.get("allowed_users", [])
    if uid not in allowed:
        allowed.append(uid)
        cfg["allowed_users"] = allowed
        save_config(cfg)

    await update.message.reply_text(
        build_users_message(allowed),
        parse_mode="Markdown",
        reply_markup=build_users_keyboard(allowed),
    )
    return MANAGE_USERS


# ── /cancel ────────────────────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    setup_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", setup_start_entry),
            CommandHandler("config", setup_config_entry),
        ],
        states={
            SETUP_PLATFORMS: [
                CallbackQueryHandler(setup_platforms_callback, pattern="^setup:"),
            ],
            SETUP_FIELD: [
                CallbackQueryHandler(setup_field_callback, pattern="^setup:keep$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, setup_field_input),
            ],
            SETUP_CONFIRM: [
                CallbackQueryHandler(setup_confirm_callback, pattern="^setup:save$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_message=False,
    )

    users_conv = ConversationHandler(
        entry_points=[CommandHandler("users", users_command)],
        states={
            MANAGE_USERS: [CallbackQueryHandler(handle_users_callback, pattern="^usr:")],
            ADD_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_user_id)],
            DELETE_USER: [CallbackQueryHandler(handle_users_callback, pattern="^usr:remove:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
        ],
        states={
            CONFIRM_NAMES: [
                CallbackQueryHandler(handle_callback),
            ],
            AWAIT_MANUAL_NAMES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_manual_names),
            ],
            AWAIT_HANDLE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_handle_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(setup_conv)
    app.add_handler(users_conv)
    app.add_handler(conv)
    app.run_polling()


if __name__ == "__main__":
    main()
