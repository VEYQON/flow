# Copyright (c) 2026, VEYQON and contributors
# License: AGPL-3.0-or-later. See license.txt

"""Which site may spend AI tokens: no AI on dev sites, and the token quota kept by the hosting
control panel. Called before every turn (flow.api) and before every model call (flow.lib.model)."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

# shortcut: one SELECT per turn (~1 ms on the local network), no cache; add a short per-site cache if it shows in timings.
QUOTA_QUERY = (
	"SELECT monthly_token_quota, bonus_credits, tokens_used, status FROM `tabSite AI Quota` WHERE site = %s"
)


def is_dev_site(site: str | None = None) -> bool:
	"""True when the site name has a `dev` label (spm.dev.veyqon.co, dev.example.com). Dev sites
	share production databases, so production keys must never be spent from them."""
	if site is None:
		site = getattr(frappe.local, "site", None)
	return "dev" in (site or "").strip().lower().split(".")


def dev_site_message() -> str:
	return _("AI is switched off on development sites. Use the production site for AI features.")


def quota_site() -> str:
	"""The quota row this site spends from: `ai_quota_site` from its site config when set (a site
	that shares another site's database), otherwise the site's own name. Matched exactly."""
	return (frappe.conf.get("ai_quota_site") or getattr(frappe.local, "site", None) or "").strip().lower()


def quota_message() -> str | None:
	"""None when this site may start or continue a turn, otherwise the message to show.

	Reads the control panel's `tabSite AI Quota` with the read-only login in `ai_quota_db`
	(common site config: host, user, password, database). Not configured, or the database not
	answering: allowed and logged, so a control-panel outage does not stop every site."""
	conf = frappe.conf.get("ai_quota_db")
	if not conf:
		return None
	site = quota_site()
	try:
		row = _quota_row(conf, site)
	except Exception:
		frappe.log_error(title="AI quota check failed", message=frappe.get_traceback())
		return None

	if row is None:
		if frappe.conf.get("ai_quota_require_row"):
			return _("AI is not set up for {0} yet: there is no token quota for this site. Please contact VEYQON support.").format(site)
		return None

	total = int(row["monthly_token_quota"] or 0) + int(row["bonus_credits"] or 0)
	if total - int(row["tokens_used"] or 0) > 0 and row["status"] != "Exhausted":
		return None
	if _is_admin():
		return _(
			"**AI Token Quota Exhausted**: The monthly token quota for {0} has been completely used (0 tokens remaining).\n\n"
			"As an administrator, you can buy more tokens for your site in the [VEYQON Client Portal](https://cloud.veyqon.co/client/billing)."
		).format(site)
	return _(
		"**AI Token Quota Limit Reached**: Your organisation's monthly AI token quota for {0} has been used up.\n\n"
		"Please ask your company administrator for an AI token top-up."
	).format(site)


def _quota_row(conf: dict[str, Any], site: str) -> dict[str, Any] | None:
	import pymysql

	conn = pymysql.connect(
		host=conf.get("host") or "db",
		port=int(conf.get("port") or 3306),
		user=conf["user"],
		password=conf["password"],
		database=conf["database"],
		connect_timeout=2,
		read_timeout=2,
		cursorclass=pymysql.cursors.DictCursor,
	)
	try:
		with conn.cursor() as cur:
			cur.execute(QUOTA_QUERY, (site,))
			return cur.fetchone()
	finally:
		conn.close()


def _is_admin() -> bool:
	user = frappe.session.user
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)
