"""Transactional emails, on brand and safe to render anywhere.

Table layout with inline styles (what every client renders), the design tokens as solid hex (clients
handle rgba badly), no remote images, tracking pixels or rewritten links, and a plain-text part. The
logo is drawn with styled cells, so it shows even where images are blocked.
Every dynamic value is escaped. One-time codes never go in the subject, which shows on lock screens.
"""

from datetime import datetime
from html import escape

from app.core.config import get_settings
from app.mail.sender import Email

BACKGROUND = "#f3f4f0"
SURFACE = "#ffffff"
FOREGROUND = "#111111"
MUTED = "#6b6b66"
BORDER = "#dadbd2"
BORDER_STRONG = "#c7c8bf"
PRIMARY = "#1d72e8"
PRIMARY_STRONG = "#1559bd"
# the logo mark's two blues
LOGO_CORE = "#055eab"
LOGO_ORBIT = "#108cf9"
# the platform's icon accents, as a stripe across the top of every card
ACCENTS = ("#1d72e8", "#7c4dff", "#d99a00", "#e8590c", "#388600")
# the raised "chrome" bottom edge the platform's buttons and cards sit on
EDGE = 4
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
# keeps subjects single-line: a header can't be split by a name carrying a newline
SUBJECT_UNSAFE = str.maketrans("", "", "".join(map(chr, range(32))) + "\x7f")


def _clean(value: str) -> str:
    return value.translate(SUBJECT_UNSAFE).strip()


def _product() -> str:
    return get_settings().product


def _a(noun: str) -> str:
    return f"{'an' if noun[0].lower() in 'aeiou' else 'a'} {noun}"


def _dot(size: int, color: str) -> str:
    return (
        f'<div style="display:inline-block;width:{size}px;height:{size}px;border-radius:50%;background:{color};'
        'font-size:0;line-height:0;">&nbsp;</div>'
    )


def _logo() -> str:
    """The mark: a core with four dots orbiting it, as a 3x3 grid of cells (24px)."""
    orbit, core = _dot(5, LOGO_ORBIT), _dot(10, LOGO_CORE)
    cell = 'style="font-size:0;line-height:0;padding:0;"'
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">'
        f'<tr><td width="7" height="7" {cell}></td><td width="10" align="center" valign="bottom" {cell}>{orbit}</td>'
        f'<td width="7" {cell}></td></tr>'
        f'<tr><td height="10" align="right" valign="middle" {cell}>{orbit}</td>'
        f'<td align="center" valign="middle" {cell}>{core}</td>'
        f'<td align="left" valign="middle" {cell}>{orbit}</td></tr>'
        f'<tr><td height="7" {cell}></td><td align="center" valign="top" {cell}>{orbit}</td>'
        f'<td {cell}></td></tr></table>'
    )


def _accent_stripe() -> str:
    last = len(ACCENTS) - 1
    cells = "".join(
        f'<td height="4" style="height:4px;font-size:0;line-height:0;background:{color};'
        f'border-radius:{"11px 0 0 0" if i == 0 else "0 11px 0 0" if i == last else "0"};">&nbsp;</td>'
        for i, color in enumerate(ACCENTS)
    )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>{cells}</tr></table>'
    )


def _layout(title: str, preheader: str, body: str, footer: str | None = None) -> str:
    product = _product()
    footer = footer or (
        f"You're receiving this because of activity on your {product} account. This mailbox isn't monitored."
    )
    brand = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td valign="middle" style="padding-right:8px;">{_logo()}</td>'
        f'<td valign="middle" style="font-family:{FONT};font-size:19px;color:{FOREGROUND};letter-spacing:-0.01em;">'
        f"{escape(product)}</td></tr></table>"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light"><title>{escape(title)}</title></head>
<body style="margin:0;padding:0;background:{BACKGROUND};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{BACKGROUND};">
<tr><td align="center" style="padding:40px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;">
<tr><td style="padding:0 4px 20px;">{brand}</td></tr>
<tr><td style="background:{SURFACE};border:1px solid {BORDER};border-bottom:{EDGE}px solid {BORDER_STRONG};
border-radius:12px;">
{_accent_stripe()}
<div style="padding:32px;font-family:{FONT};font-size:15px;line-height:1.6;color:{FOREGROUND};">
<h1 style="margin:0 0 16px;font-size:22px;line-height:1.3;font-weight:700;letter-spacing:-0.01em;">{escape(title)}</h1>
{body}
</div>
</td></tr>
<tr><td style="padding:20px 4px 0;font-family:{FONT};font-size:12px;line-height:1.5;color:{MUTED};">
{escape(footer)}
</td></tr>
</table></td></tr></table></body></html>"""


def _paragraph(text: str) -> str:
    return f'<p style="margin:0 0 16px;">{text}</p>'


def _muted(text: str) -> str:
    return f'<p style="margin:16px 0 0;font-size:13px;color:{MUTED};">{text}</p>'


def _code_block(code: str) -> str:
    return (
        f'<div style="margin:8px 0 20px;padding:16px;background:{BACKGROUND};border:1px solid {BORDER};'
        f'border-bottom:{EDGE}px solid {BORDER_STRONG};border-radius:8px;text-align:center;font-family:{MONO};'
        f'font-size:30px;font-weight:600;letter-spacing:8px;color:{PRIMARY_STRONG};">'
        f"{escape(code)}</div>"
    )


def _button(label: str, url: str) -> str:
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 20px;"><tr>'
        f'<td style="background:{PRIMARY};border:1px solid {PRIMARY_STRONG};border-bottom:{EDGE}px solid {PRIMARY_STRONG};'
        'border-radius:8px;">'
        f'<a href="{escape(url)}" style="display:inline-block;padding:11px 20px;font-family:{FONT};font-size:15px;'
        f'font-weight:600;color:#ffffff;text-decoration:none;">{escape(label)}</a></td></tr></table>'
    )


def _code_email(to: str, subject: str, intro: str, code: str, minutes: int, ignore: str) -> Email:
    html = _layout(subject, f"Your code expires in {minutes} minutes.", "".join([
        _paragraph(escape(intro)),
        _code_block(code),
        _paragraph(f"This code expires in {minutes} minutes and can be used once."),
        _muted(escape(ignore)),
    ]))
    text = f"{subject}\n\n{intro}\n\n    {code}\n\nThis code expires in {minutes} minutes and can be used once.\n\n{ignore}\n"
    return Email(to=to, subject=subject, html=html, text=text)


def verification_email(to: str, code: str, minutes: int) -> Email:
    return _code_email(
        to, f"Verify your email for {_product()}", "Enter this code to finish creating your account:", code, minutes,
        "Didn't sign up? You can ignore this email; no account is created without the code.",
    )


def password_reset_email(to: str, code: str, minutes: int) -> Email:
    return _code_email(
        to, f"Reset your {_product()} password", "Enter this code to choose a new password:", code, minutes,
        "Didn't ask for this? Ignore this email; your password hasn't changed.",
    )


def add_address_email(to: str, code: str, minutes: int) -> Email:
    product = _product()
    return _code_email(
        to, f"Add this email to your {product} account", f"Enter this code to add this address to your {product} account:",
        code, minutes, "Didn't ask for this? Ignore this email; nothing changes without the code. If this address already "
        f"has {_a(product)} account, you'll be offered to merge the two.",
    )


def accounts_merged_email(to: str, username: str | None) -> Email:
    origin = get_settings().frontend_origin
    product = _product()
    subject = f"Your {product} accounts were merged"
    who = f" into @{_clean(username)}" if username else ""
    settings_url = f"{origin}/settings/profile"
    html = _layout(subject, f"Two {product} accounts are now one.", "".join([
        _paragraph(f"Two {product} accounts that sign in with this address were merged{escape(who)}. Your workspaces, "
                   "repositories, documents and tickets are all in the merged account."),
        _button("Review sign-in methods", settings_url),
        _muted("If you didn't do this, reset your password and sign out everywhere from your profile settings."),
    ]))
    text = (
        f"{subject}\n\nTwo {product} accounts that sign in with this address were merged{who}. Your workspaces, "
        f"repositories, documents and tickets are all in the merged account.\n\nReview sign-in methods: {settings_url}\n\n"
        "If you didn't do this, reset your password and sign out everywhere from your profile settings.\n"
    )
    return Email(to=to, subject=subject, html=html, text=text)


def account_exists_email(to: str) -> Email:
    origin = get_settings().frontend_origin
    product = _product()
    subject = f"You already have {_a(product)} account"
    sign_in, forgot = f"{origin}/sign-in", f"{origin}/forgot-password"
    html = _layout(subject, "Someone tried to sign up with this email.", "".join([
        _paragraph(f"Someone tried to create {_a(product)} account with this email address, but it already has one."),
        _button("Sign in", sign_in),
        _paragraph(f'Forgot your password? <a href="{escape(forgot)}" style="color:{PRIMARY};">Reset it here</a>.'),
        _muted("If this wasn't you, no action is needed; your account hasn't changed."),
    ]))
    text = (
        f"{subject}\n\nSomeone tried to create {_a(product)} account with this email address, but it already has one.\n\n"
        f"Sign in: {sign_in}\nForgot your password? {forgot}\n\nIf this wasn't you, no action is needed.\n"
    )
    return Email(to=to, subject=subject, html=html, text=text)


def workspace_invite_email(to: str, inviter: str, workspace: str, role: str, url: str, expires_at: datetime) -> Email:
    inviter, workspace = _clean(inviter), _clean(workspace)
    subject = f"{inviter} invited you to {workspace} on {_product()}"
    expires = expires_at.strftime("%B %-d, %Y")
    html = _layout(subject, f"Join {workspace} as {role}.", "".join([
        _paragraph(f"<strong>{escape(inviter)}</strong> invited you to join the <strong>{escape(workspace)}</strong> "
                   f"workspace as {escape(role)}."),
        _button("Join workspace", url),
        _paragraph(f'Or paste this link into your browser:<br><span style="word-break:break-all;color:{MUTED};font-size:13px;">'
                   f"{escape(url)}</span>"),
        _muted(f"This invite expires on {escape(expires)}. Not expecting it? You can ignore this email."),
    ]))
    text = (
        f"{subject}\n\n{inviter} invited you to join the {workspace} workspace as {role}.\n\n"
        f"Join: {url}\n\nThis invite expires on {expires}. Not expecting it? You can ignore this email.\n"
    )
    return Email(to=to, subject=subject, html=html, text=text)


def waitlist_email(to: str, name: str) -> Email:
    product = _product()
    first_name = _clean(name).split()[0]
    subject = f"You're on the {product} waitlist"
    lines = [
        f"Hi {first_name},",
        f"Thanks so much for signing up for {product}. I'm Bilal, and I'm building it because I got tired of "
        "decisions getting lost between Slack threads, tickets, docs and meetings. Seeing your name come in "
        "genuinely made my day.",
        "You're officially on the list. We're opening a small private beta soon, and I'll write to you myself as "
        "soon as there's a spot for you.",
        f"If there's one thing you'd love {product} to do for your team, just hit reply. I read every one.",
    ]
    sign_off = ["Talk soon,", "Bilal", f"Founder, {product}"]
    footer = f"You're getting this because you joined the {product} waitlist. Not you? Just ignore this email."
    html = _layout(
        "You're on the list", "Thanks for signing up. Here's what happens next.",
        "".join(_paragraph(escape(line)) for line in lines) + _paragraph("<br>".join(map(escape, sign_off))), footer,
    )
    text = "\n\n".join([subject, *lines, "\n".join(sign_off), footer]) + "\n"
    return Email(to=to, subject=subject, html=html, text=text)
