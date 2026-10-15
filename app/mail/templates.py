"""Transactional emails, on brand and safe to render anywhere.

Table layout with inline styles (what every client renders), the design tokens as solid hex (clients
handle rgba badly), no remote images, tracking pixels or rewritten links, and a plain-text part.
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
PRIMARY = "#1d72e8"
PRIMARY_STRONG = "#1559bd"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
# keeps subjects single-line: a header can't be split by a name carrying a newline
SUBJECT_UNSAFE = str.maketrans("", "", "".join(map(chr, range(32))) + "\x7f")


def _clean(value: str) -> str:
    return value.translate(SUBJECT_UNSAFE).strip()


def _layout(title: str, preheader: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light"><title>{escape(title)}</title></head>
<body style="margin:0;padding:0;background:{BACKGROUND};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{BACKGROUND};">
<tr><td align="center" style="padding:40px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;">
<tr><td style="padding:0 4px 20px;font-family:{FONT};font-size:18px;font-weight:700;color:{FOREGROUND};letter-spacing:-0.01em;">Overwatch</td></tr>
<tr><td style="background:{SURFACE};border:1px solid {BORDER};border-radius:12px;padding:32px;font-family:{FONT};font-size:15px;line-height:1.6;color:{FOREGROUND};">
<h1 style="margin:0 0 16px;font-size:20px;line-height:1.3;font-weight:600;">{escape(title)}</h1>
{body}
</td></tr>
<tr><td style="padding:20px 4px 0;font-family:{FONT};font-size:12px;line-height:1.5;color:{MUTED};">
You're receiving this because of activity on your Overwatch account. This mailbox isn't monitored.
</td></tr>
</table></td></tr></table></body></html>"""


def _paragraph(text: str) -> str:
    return f'<p style="margin:0 0 16px;">{text}</p>'


def _muted(text: str) -> str:
    return f'<p style="margin:16px 0 0;font-size:13px;color:{MUTED};">{text}</p>'


def _code_block(code: str) -> str:
    return (
        f'<div style="margin:8px 0 20px;padding:16px;background:{BACKGROUND};border:1px solid {BORDER};border-radius:8px;'
        f'text-align:center;font-family:{MONO};font-size:30px;font-weight:600;letter-spacing:8px;color:{FOREGROUND};">'
        f"{escape(code)}</div>"
    )


def _button(label: str, url: str) -> str:
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 20px;"><tr>'
        f'<td style="background:{PRIMARY};border:1px solid {PRIMARY_STRONG};border-radius:8px;">'
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
        to, "Verify your email for Overwatch", "Enter this code to finish creating your account:", code, minutes,
        "Didn't sign up? You can ignore this email; no account is created without the code.",
    )


def password_reset_email(to: str, code: str, minutes: int) -> Email:
    return _code_email(
        to, "Reset your Overwatch password", "Enter this code to choose a new password:", code, minutes,
        "Didn't ask for this? Ignore this email; your password hasn't changed.",
    )


def account_exists_email(to: str) -> Email:
    origin = get_settings().frontend_origin
    subject = "You already have an Overwatch account"
    sign_in, forgot = f"{origin}/sign-in", f"{origin}/forgot-password"
    html = _layout(subject, "Someone tried to sign up with this email.", "".join([
        _paragraph("Someone tried to create an Overwatch account with this email address, but it already has one."),
        _button("Sign in", sign_in),
        _paragraph(f'Forgot your password? <a href="{escape(forgot)}" style="color:{PRIMARY};">Reset it here</a>.'),
        _muted("If this wasn't you, no action is needed; your account hasn't changed."),
    ]))
    text = (
        f"{subject}\n\nSomeone tried to create an Overwatch account with this email address, but it already has one.\n\n"
        f"Sign in: {sign_in}\nForgot your password? {forgot}\n\nIf this wasn't you, no action is needed.\n"
    )
    return Email(to=to, subject=subject, html=html, text=text)


def workspace_invite_email(to: str, inviter: str, workspace: str, role: str, url: str, expires_at: datetime) -> Email:
    inviter, workspace = _clean(inviter), _clean(workspace)
    subject = f"{inviter} invited you to {workspace} on Overwatch"
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
