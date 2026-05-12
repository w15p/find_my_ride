from __future__ import annotations

import logging
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List
from urllib.parse import urlparse

from jinja2 import BaseLoader, Environment

from core.currency import to_usd_str
from core.models import Listing

log = logging.getLogger(__name__)


# Re-export the legacy name so other modules can keep importing notifier.to_usd
def to_usd(price_value, currency):
    return to_usd_str(price_value, currency)


_HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { font-family: Arial, sans-serif; background: #f5f5f5; margin: 0; padding: 20px; }
  h2 { color: #c0392b; }
  .listing {
    background: white;
    border: 1px solid #ddd;
    border-radius: 6px;
    padding: 14px;
    margin: 12px 0;
    display: flex;
    gap: 16px;
    align-items: flex-start;
  }
  .listing img {
    width: 200px;
    height: 140px;
    object-fit: cover;
    border-radius: 4px;
    flex-shrink: 0;
  }
  .listing .no-image {
    width: 200px;
    height: 140px;
    background: #eee;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #aaa;
    font-size: 12px;
    border-radius: 4px;
    flex-shrink: 0;
  }
  .details { flex: 1; }
  .details a.title { font-size: 16px; font-weight: bold; color: #2c3e50; text-decoration: none; }
  .details a.title:hover { text-decoration: underline; }
  .meta { color: #666; font-size: 13px; margin-top: 6px; }
  .desc { color: #333; font-size: 13px; margin-top: 8px; line-height: 1.4; }
  .price { color: #27ae60; font-weight: bold; font-size: 15px; margin-top: 4px; }
  .site { background: #ecf0f1; border-radius: 3px; padding: 2px 6px; font-size: 11px; color: #555; }
  .also-on { color: #888; font-size: 12px; margin-top: 6px; }
  .also-on a { color: #555; }
</style>
</head>
<body>
<h2>Ford Escort Mk1 LHD — {{ count }} new listing{{ 's' if count != 1 else '' }}</h2>
{% for l in listings %}
<div class="listing">
  {% if l.image_url %}
    <img src="{{ l.image_url }}" alt="{{ l.title | e }}" loading="lazy">
  {% else %}
    <div class="no-image">No image</div>
  {% endif %}
  <div class="details">
    <a class="title" href="{{ l.url }}" target="_blank">{{ l.title | e }}</a>
    <div class="price">
      {{ l.price or 'POA' }}
      {% if l._usd %}&nbsp;<span style="color:#888;font-size:13px;">({{ l._usd }})</span>{% endif %}
    </div>
    <div class="meta">
      Year: {{ l.year or '?' }}&nbsp;&nbsp;|&nbsp;&nbsp;
      Location: {{ l.location or '?' }}&nbsp;&nbsp;|&nbsp;&nbsp;
      Drive: {{ (l.steering or '?') | upper }}&nbsp;&nbsp;
      <span class="site">{{ l.site_name }}</span>
    </div>
    {% if l._description %}
    <div class="desc">{{ l._description | e }}</div>
    {% endif %}
    {% if l._also_on %}
    <div class="also-on">Also on:
      {% for src in l._also_on %}<a href="{{ src.url }}" target="_blank">{{ src.site_name }}</a>{% if not loop.last %}, {% endif %}{% endfor %}
    </div>
    {% endif %}
  </div>
</div>
{% endfor %}
</body>
</html>
"""

_PLAIN_TEMPLATE = """Ford Escort Mk1 LHD — {{ count }} new listing{{ 's' if count != 1 else '' }}

{% for l in listings %}
{{ loop.index }}. {{ l.title }}
   Price:    {{ l.price or 'POA' }}{% if l._usd %} ({{ l._usd }}){% endif %}
   Year:     {{ l.year or '?' }}
   Location: {{ l.location or '?' }}
   Drive:    {{ (l.steering or '?') | upper }}
   Site:     {{ l.site_name }}
{%- if l._description %}
   Notes:    {{ l._description }}
{%- endif %}
   URL:      {{ l.url }}
{%- if l._also_on %}
   Also on:  {% for src in l._also_on %}{{ src.site_name }} ({{ src.url }}){% if not loop.last %}; {% endif %}{% endfor %}
{%- endif %}

{% endfor %}
"""


def _summarise(text: str, limit: int = 280) -> str:
    """Trim a long description to one paragraph cut at a sentence/word boundary."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    # Prefer cutting at the last sentence boundary inside the window
    for sep in ". ", "! ", "? ":
        idx = cut.rfind(sep)
        if idx > limit * 0.6:
            return cut[: idx + 1] + " …"
    # Otherwise at the last word boundary
    idx = cut.rfind(" ")
    if idx > 0:
        cut = cut[:idx]
    return cut + " …"


def _short_url(url: str) -> str:
    """Short display form for an "Also on" link — site + final path segment."""
    try:
        p = urlparse(url)
        last = p.path.rstrip("/").split("/")[-1] or p.netloc
        return last[:32]
    except Exception:
        return url[:32]


class EmailNotifier:
    def __init__(self, config: dict) -> None:
        self.smtp_host = config.get("smtp_host", "smtp.gmail.com")
        self.smtp_port = int(config.get("smtp_port", 587))
        self.smtp_user = config.get("smtp_user", "")
        self.smtp_pass = config.get("smtp_pass", "")
        self.from_addr = config.get("from_addr") or self.smtp_user
        self.to_addrs: list = config.get("to_addrs", [])

    def send_digest(
        self,
        listings: List[Listing],
        duplicates_by_canonical: dict[str, List[Listing]] | None = None,
    ) -> None:
        """Send the daily digest.

        `duplicates_by_canonical` maps a canonical listing's URL → list of
        Listing objects that were detected as duplicates of it (cross-source
        merges). They're rendered as a "Also on:" footer on the canonical card.
        """
        if not listings:
            return
        if not self.to_addrs:
            raise ValueError("No recipient addresses configured (notification.email.to_addrs)")

        duplicates_by_canonical = duplicates_by_canonical or {}

        for l in listings:
            l._usd = to_usd_str(l.price_value, l.price_currency)  # type: ignore[attr-defined]
            l._description = _summarise(l.description) if l.description else None  # type: ignore[attr-defined]
            dups = duplicates_by_canonical.get(l.url, [])
            l._also_on = [  # type: ignore[attr-defined]
                {"site_name": d.site_name, "url": d.url, "short": _short_url(d.url)}
                for d in dups
            ] or None

        env = Environment(loader=BaseLoader(), autoescape=False)
        ctx = {"listings": listings, "count": len(listings)}

        html_body = env.from_string(_HTML_TEMPLATE).render(**ctx)
        plain_body = env.from_string(_PLAIN_TEMPLATE).render(**ctx)

        subject = f"[Escort Mk1] {len(listings)} new listing{'s' if len(listings) != 1 else ''}"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)
        msg.attach(MIMEText(plain_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(self.smtp_user, self.smtp_pass)
            smtp.sendmail(self.from_addr, self.to_addrs, msg.as_string())
