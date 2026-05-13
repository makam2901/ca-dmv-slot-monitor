#!/usr/bin/env python3
"""
Poll the CA DMV appointment flow in Chrome, detect when time slots appear, and send email alerts.

Requires: Google Chrome, Python 3.10+, credentials in .env (see env.example).
"""

from __future__ import annotations

import email.utils
import json
import logging
import os
import re
import smtplib
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

LOG = logging.getLogger("dmv_monitor")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(ROOT / "monitor.log", encoding="utf-8"),
        ],
    )


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    return int(v)


def _split_csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [p.strip() for p in raw.split(",") if p.strip()]


CATEGORY_LABEL_SNIPPETS: dict[str, str] = {
    "automobile": "Automobile",
    "commercial": "Commercial",
    "motorcycle": "Motorcycle",
}


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    mail_from: str
    mail_to: str


def load_smtp() -> SmtpConfig | None:
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    mail_from = os.getenv("ALERT_EMAIL_FROM", "").strip()
    mail_to = os.getenv("ALERT_EMAIL_TO", "").strip()
    if not (host and user and password and mail_from and mail_to):
        return None
    return SmtpConfig(
        host=host,
        port=_env_int("SMTP_PORT", 587),
        user=user,
        password=password,
        mail_from=mail_from,
        mail_to=mail_to,
    )


def send_email_alert(subject: str, body: str) -> None:
    cfg = load_smtp()
    if not cfg:
        LOG.warning("SMTP not fully configured; skipping email.")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.mail_from
    msg["To"] = cfg.mail_to
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.set_content(body)
    with smtplib.SMTP(cfg.host, cfg.port, timeout=60) as smtp:
        smtp.starttls()
        smtp.login(cfg.user, cfg.password)
        smtp.send_message(msg)
    LOG.info("Sent alert email to %s", cfg.mail_to)


def _cooldown_path() -> Path:
    return ROOT / ".last_alert_ts"


def within_alert_cooldown() -> bool:
    minutes = _env_int("ALERT_COOLDOWN_MINUTES", 30)
    path = _cooldown_path()
    if not path.exists():
        return False
    try:
        last = float(path.read_text().strip())
    except ValueError:
        return False
    return (time.time() - last) < minutes * 60


def mark_alert_sent() -> None:
    _cooldown_path().write_text(str(time.time()), encoding="utf-8")


def build_chrome_driver() -> webdriver.Chrome:
    opts = ChromeOptions()
    if _env_bool("HEADLESS", False):
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--lang=en-US,en")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    chrome_bin = os.getenv("CHROME_BINARY", "").strip()
    if chrome_bin:
        opts.binary_location = chrome_bin
    elif sys.platform == "darwin":
        default_mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.isfile(default_mac):
            opts.binary_location = default_mac

    service = ChromeService()
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(_env_int("PAGE_LOAD_TIMEOUT_SECONDS", 45))
    driver.implicitly_wait(_env_int("IMPLICIT_WAIT_SECONDS", 8))
    return driver


def safe_quit(driver: webdriver.Chrome | None) -> None:
    if not driver:
        return
    try:
        driver.quit()
    except Exception:
        LOG.debug("driver.quit failed", exc_info=True)


def click_category(driver: webdriver.Chrome, category: str) -> None:
    valid = {"real_id_cdl", *CATEGORY_LABEL_SNIPPETS}
    if category not in valid:
        raise ValueError(
            f"Unknown APPOINTMENT_CATEGORY={category!r}. "
            f"Expected one of: {', '.join(sorted(valid))}"
        )
    if category == "real_id_cdl":
        xpath = (
            "//*[self::label or self::span or self::div]"
            "[contains(normalize-space(.),'REAL ID') and "
            "contains(normalize-space(.),'CDL')]"
        )
    else:
        snippet = CATEGORY_LABEL_SNIPPETS[category]
        xpath = (
            f"//*[self::label or self::span or self::div]"
            f"[contains(normalize-space(.), {json.dumps(snippet)})]"
        )
    el = WebDriverWait(driver, 25).until(EC.element_to_be_clickable((By.XPATH, xpath)))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
    el.click()


def find_dl_input(driver: webdriver.Chrome):
    candidates = [
        (By.XPATH, "//input[contains(@placeholder,'1234567')]"),
        (By.XPATH, "//input[contains(translate(@name,'LICENSE','license'),'license')]"),
        (By.XPATH, "//input[contains(@name,'License') or contains(@name,'license')]"),
    ]
    for by, sel in candidates:
        try:
            return driver.find_element(by, sel)
        except NoSuchElementException:
            continue
    raise NoSuchElementException("Could not find driver's license input")


def find_dob_input(driver: webdriver.Chrome):
    candidates = [
        (By.XPATH, "//input[contains(@placeholder,'mm/dd')]"),
        (By.XPATH, "//input[contains(@placeholder,'MM/DD')]"),
        (By.CSS_SELECTOR, "input[type='date']"),
    ]
    for by, sel in candidates:
        try:
            return driver.find_element(by, sel)
        except NoSuchElementException:
            continue
    raise NoSuchElementException("Could not find date of birth input")


def fill_if_present(driver: webdriver.Chrome, by: By, selector: str, value: str) -> bool:
    if not value:
        return False
    try:
        el = driver.find_element(by, selector)
    except NoSuchElementException:
        return False
    el.clear()
    el.send_keys(value)
    return True


def submit_make_appointment(driver: webdriver.Chrome) -> None:
    btn = WebDriverWait(driver, 25).until(
        EC.element_to_be_clickable(
            (By.XPATH, "//button[contains(.,'Make an Appointment')]")
        )
    )
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
    btn.click()


def _xpath_literal_single_quoted(s: str) -> str:
    """XPath 1.0 string in single quotes; escape ' as ''."""
    return "'" + s.replace("'", "''") + "'"


def optional_zip_search(driver: webdriver.Chrome) -> None:
    z = os.getenv("DMV_ZIP_CODE", "").strip()
    if not z:
        return
    xpaths = [
        "//input[contains(translate(@placeholder,'ZIP','zip'),'zip')]",
        "//input[contains(@name,'zip')]",
        "//input[@inputmode='numeric' and contains(@aria-label,'Zip')]",
    ]
    for xp in xpaths:
        try:
            inp = driver.find_element(By.XPATH, xp)
        except NoSuchElementException:
            continue
        inp.clear()
        inp.send_keys(z)
        for btn_text in ("Search", "Continue", "Next", "Submit"):
            try:
                b = driver.find_element(
                    By.XPATH, f"//button[contains(.,{json.dumps(btn_text)})]"
                )
                if b.is_displayed():
                    b.click()
                    return
            except NoSuchElementException:
                continue
        return


def optional_select_office(driver: webdriver.Chrome) -> None:
    """If DMV_OFFICE_CLICK_TEXT is set, click the first visible control whose text contains it (e.g. Santa Clara)."""
    label = os.getenv("DMV_OFFICE_CLICK_TEXT", "").strip()
    if not label:
        return
    time.sleep(float(os.getenv("OFFICE_LIST_PAUSE", "1.0")))
    lit = _xpath_literal_single_quoted(label.lower())
    # Case-insensitive match on normalized text
    contains_ci = (
        f"contains(translate(normalize-space(.), "
        f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {lit})"
    )
    xpaths = [
        f"//a[{contains_ci}]",
        f"//button[{contains_ci}]",
        f"//*[@role='button' and {contains_ci}]",
        f"//tr[{contains_ci}]//*[self::a or self::button][1]",
        f"//li[{contains_ci}]//*[self::a or self::button][1]",
    ]
    for xp in xpaths:
        try:
            els = driver.find_elements(By.XPATH, xp)
        except WebDriverException:
            continue
        for el in els:
            try:
                if el.is_displayed() and el.is_enabled():
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});", el
                    )
                    el.click()
                    time.sleep(
                        float(os.getenv("POST_OFFICE_CLICK_PAUSE", "1.5"))
                    )
                    return
            except StaleElementReferenceException:
                continue


def page_body_text(driver: webdriver.Chrome) -> str:
    try:
        return driver.find_element(By.TAG_NAME, "body").text.lower()
    except StaleElementReferenceException:
        return ""


def has_negative_signal(body: str) -> bool:
    phrases = _split_csv(
        "NEGATIVE_PHRASES",
        "no appointments available,no appointment times,there are currently no appointments,"
        "no times available,fully booked,try again later",
    )
    return any(p.lower() in body for p in phrases)


def count_visible_slot_elements(driver: webdriver.Chrome) -> int:
    raw = os.getenv("SLOT_CSS_SELECTORS", "").strip()
    if not raw:
        return 0
    min_hits = 0
    for sel in [s.strip() for s in raw.split(",") if s.strip()]:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
        except WebDriverException:
            continue
        for e in els:
            try:
                if e.is_displayed() and e.is_enabled():
                    min_hits += 1
            except StaleElementReferenceException:
                continue
    return min_hits


def time_pattern_suggest_slots(body: str) -> bool:
    return bool(re.search(r"\b\d{1,2}:\d{2}\s*(am|pm)\b", body))


_RE_TIME_HM = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm)\b", re.I)
_RE_TIME_H = re.compile(r"\b(\d{1,2})\s*(am|pm)\b", re.I)

_RE_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_RE_DATE_US = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b")
_RE_DATE_MON = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(\d{4})\b",
    re.I,
)
_RE_DATE_LONG = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2}),?\s+(\d{4})\b",
    re.I,
)


def _clock_to_minutes(hour12: int, minute: int, ampm: str) -> int:
    ap = ampm.lower()
    h = int(hour12)
    mi = int(minute)
    if ap == "am":
        if h == 12:
            h = 0
    else:
        if h != 12:
            h += 12
    return h * 60 + mi


def _spans_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    (s1, e1), (s2, e2) = a, b
    return not (e1 <= s2 or s1 >= e2)


def parse_times_from_text(text: str) -> set[int]:
    """Parse 12-hour times in text to minutes since local midnight."""
    out: set[int] = set()
    hm_spans: list[tuple[int, int]] = []
    for m in _RE_TIME_HM.finditer(text):
        out.add(_clock_to_minutes(int(m.group(1)), int(m.group(2)), m.group(3)))
        hm_spans.append(m.span())
    for m in _RE_TIME_H.finditer(text):
        span = m.span()
        if any(_spans_overlap(span, o) for o in hm_spans):
            continue
        out.add(_clock_to_minutes(int(m.group(1)), 0, m.group(2)))
    return out


def _format_minutes_clock(m: int) -> str:
    h, mi = divmod(m, 60)
    ap = "am" if h < 12 else "pm"
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{mi:02d} {ap}"


def page_body_text_raw(driver: webdriver.Chrome) -> str:
    try:
        return driver.find_element(By.TAG_NAME, "body").text
    except StaleElementReferenceException:
        return ""


def gather_slot_parsing_text(driver: webdriver.Chrome) -> str:
    parts = [page_body_text_raw(driver)]
    raw = os.getenv("SLOT_CSS_SELECTORS", "").strip()
    if raw:
        for sel in [s.strip() for s in raw.split(",") if s.strip()]:
            try:
                for e in driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if e.is_displayed():
                            t = (e.text or "").strip()
                            if t:
                                parts.append(t)
                    except StaleElementReferenceException:
                        continue
            except WebDriverException:
                continue
    return "\n".join(parts)


def collect_slot_times_minutes(driver: webdriver.Chrome) -> set[int]:
    return parse_times_from_text(gather_slot_parsing_text(driver))


def times_in_target_window(times: set[int]) -> list[int]:
    if not times:
        return []
    hour_24 = _env_int("SLOT_TARGET_HOUR_24", 13)
    win = _env_int("SLOT_TARGET_WINDOW_MINUTES", 45)
    center = hour_24 * 60
    lo, hi = center - win, center + win
    return sorted(t for t in times if lo <= t <= hi)


def slot_times_within_target_window(times: set[int]) -> bool:
    return bool(times_in_target_window(times))


def _date_window_for_threshold(threshold: date) -> tuple[date, date]:
    """Only keep calendar years around the cutoff (drops DOB years like 2000 on the page)."""
    lo = date(threshold.year - 1, 1, 1)
    hi = date(threshold.year, 12, 31)
    return lo, hi


def parse_alert_date_before() -> date | None:
    raw = os.getenv("SLOT_ALERT_IF_DATE_BEFORE", "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        LOG.error(
            "Invalid SLOT_ALERT_IF_DATE_BEFORE=%r; expected YYYY-MM-DD. Date filter disabled.",
            raw,
        )
        return None


def parse_dates_from_text(text: str, threshold: date) -> set[date]:
    lo, hi = _date_window_for_threshold(threshold)
    found: set[date] = set()

    for m in _RE_DATE_ISO.finditer(text):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            dd = date(y, mo, d)
        except ValueError:
            continue
        if lo <= dd <= hi:
            found.add(dd)

    for m in _RE_DATE_US.finditer(text):
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            dd = date(y, mo, d)
        except ValueError:
            continue
        if lo <= dd <= hi:
            found.add(dd)

    mon_map = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    for m in _RE_DATE_MON.finditer(text):
        mon_s = m.group(1)[:3].lower()
        d, y = int(m.group(2)), int(m.group(3))
        mo = mon_map.get(mon_s)
        if not mo:
            continue
        try:
            dd = date(y, mo, d)
        except ValueError:
            continue
        if lo <= dd <= hi:
            found.add(dd)

    for m in _RE_DATE_LONG.finditer(text):
        try:
            dd = datetime.strptime(
                f"{m.group(1)} {int(m.group(2))}, {m.group(3)}", "%B %d, %Y"
            ).date()
        except ValueError:
            continue
        if lo <= dd <= hi:
            found.add(dd)

    return found


def collect_slot_dates_for_filter(driver: webdriver.Chrome, threshold: date) -> set[date]:
    return parse_dates_from_text(gather_slot_parsing_text(driver), threshold)


def dates_strictly_before_threshold(
    driver: webdriver.Chrome, threshold: date
) -> list[date]:
    return sorted({d for d in collect_slot_dates_for_filter(driver, threshold) if d < threshold})


def slots_likely_available(driver: webdriver.Chrome) -> bool:
    body = page_body_text(driver)
    if not body.strip():
        return False
    if has_negative_signal(body):
        return False
    need = _env_int("SLOT_MIN_COUNT", 1)
    n = count_visible_slot_elements(driver)
    has_css = bool(os.getenv("SLOT_CSS_SELECTORS", "").strip())

    if n >= need:
        base = True
    elif has_css:
        base = False
    else:
        base = time_pattern_suggest_slots(body)

    if not base:
        return False

    if _env_bool("SLOT_FILTER_NEAR_HOUR_LOCAL_ENABLED", False):
        times = collect_slot_times_minutes(driver)
        if not times:
            LOG.info(
                "Local time filter is on but no times could be parsed; not alerting "
                "(set SLOT_CSS_SELECTORS so slot button text is scanned, or check page copy)."
            )
            return False
        if not slot_times_within_target_window(times):
            preview = ", ".join(_format_minutes_clock(t) for t in sorted(times)[:24])
            suffix = " …" if len(times) > 24 else ""
            LOG.info(
                "Slots or times on page, but none within target window "
                "(hour=%s, ±%s min). Parsed: %s%s",
                _env_int("SLOT_TARGET_HOUR_24", 13),
                _env_int("SLOT_TARGET_WINDOW_MINUTES", 45),
                preview,
                suffix,
            )
            return False

    cutoff = parse_alert_date_before()
    if cutoff is not None:
        in_window = collect_slot_dates_for_filter(driver, cutoff)
        earlier = [d for d in in_window if d < cutoff]
        if not earlier:
            preview = ", ".join(str(d) for d in sorted(in_window)[:12])
            suffix = " …" if len(in_window) > 12 else ""
            LOG.info(
                "Date filter: need a parsed appointment date strictly before %s. "
                "Calendar-range dates seen: %s%s",
                cutoff,
                preview or "(none)",
                suffix,
            )
            return False

    return True


def run_flow(driver: webdriver.Chrome) -> None:
    start = os.getenv(
        "START_URL",
        "https://www.dmv.ca.gov/portal/appointments/select-appointment-type",
    ).strip()
    driver.get(start)

    category = os.getenv("APPOINTMENT_CATEGORY", "automobile").strip().lower()
    click_category(driver, category)

    dl = os.getenv("DMV_DL_NUMBER", "").strip()
    dob = os.getenv("DMV_DOB", "").strip()
    if not dl or not dob:
        raise RuntimeError("Set DMV_DL_NUMBER and DMV_DOB in .env to continue past service selection.")

    dl_el = find_dl_input(driver)
    dob_el = find_dob_input(driver)
    dl_el.clear()
    dl_el.send_keys(dl)
    dob_el.clear()
    dob_el.send_keys(dob)

    time.sleep(float(os.getenv("PRE_SUBMIT_PAUSE", "0.4")))
    submit_make_appointment(driver)

    WebDriverWait(driver, 45).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    time.sleep(float(os.getenv("POST_NAV_PAUSE", "2.0")))
    optional_zip_search(driver)
    time.sleep(float(os.getenv("POST_ZIP_PAUSE", "1.5")))
    optional_select_office(driver)


def recover_session(driver_holder: list[webdriver.Chrome | None]) -> webdriver.Chrome:
    safe_quit(driver_holder[0])
    driver_holder[0] = None
    d = build_chrome_driver()
    driver_holder[0] = d
    return d


def validate_required_env() -> None:
    """Exit early with a clear message instead of opening Chrome in a restart loop."""
    missing = []
    if not os.getenv("DMV_DL_NUMBER", "").strip():
        missing.append("DMV_DL_NUMBER")
    if not os.getenv("DMV_DOB", "").strip():
        missing.append("DMV_DOB")
    if missing:
        LOG.error(
            "Missing required .env values: %s. Copy env.example to .env and set them.",
            ", ".join(missing),
        )
        sys.exit(2)


def main() -> None:
    _setup_logging()
    validate_required_env()
    poll = _env_int("POLL_INTERVAL_SECONDS", 120)
    driver_box: list[webdriver.Chrome | None] = [None]

    LOG.info("Starting CA DMV slot monitor (poll every %ss).", poll)

    while True:
        driver: webdriver.Chrome | None = driver_box[0]
        try:
            if driver is None:
                driver = build_chrome_driver()
                driver_box[0] = driver
            assert driver is not None
            run_flow(driver)
            available = slots_likely_available(driver)
            LOG.info("Checked availability: %s", "OPEN" if available else "none")

            if available and not within_alert_cooldown():
                url = driver.current_url
                snippet = page_body_text(driver)[:4000]
                extra = ""
                cutoff = parse_alert_date_before()
                if cutoff is not None:
                    earlier_dates = dates_strictly_before_threshold(driver, cutoff)
                    if earlier_dates:
                        extra += (
                            "\nParsed date(s) strictly before your cutoff "
                            f"({cutoff}): "
                            + ", ".join(str(d) for d in earlier_dates)
                            + "\n"
                        )
                if _env_bool("SLOT_FILTER_NEAR_HOUR_LOCAL_ENABLED", False):
                    matched = times_in_target_window(collect_slot_times_minutes(driver))
                    if matched:
                        ts = ", ".join(_format_minutes_clock(t) for t in matched)
                        extra += (
                            f"\nTimes in your configured window "
                            f"(hour {_env_int('SLOT_TARGET_HOUR_24', 13)}:00 local "
                            f"±{_env_int('SLOT_TARGET_WINDOW_MINUTES', 45)} min): {ts}\n"
                        )
                send_email_alert(
                    subject="CA DMV: appointment slots may be available",
                    body=(
                        "The monitor detected possible open slots.\n\n"
                        f"URL: {url}\n"
                        f"{extra}\n"
                        "Page text excerpt (lowercase):\n"
                        f"{snippet}\n"
                    ),
                )
                mark_alert_sent()
        except TimeoutException as e:
            LOG.warning("Timeout; restarting browser session. %s", e)
            driver = recover_session(driver_box)
        except WebDriverException as e:
            LOG.warning("WebDriver error; restarting session. %s", e)
            traceback.print_exc()
            driver = recover_session(driver_box)
        except Exception:
            LOG.exception("Unexpected error; restarting session in next iteration.")
            traceback.print_exc()
            recover_session(driver_box)

        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        LOG.info("Stopped by user.")
