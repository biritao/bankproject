import csv
import hashlib
import io
import os
import random
import re
import string
from datetime import date, timedelta

import mysql.connector
import streamlit as st

ACCOUNT_TYPES = ["Savings", "Checking", "Business"]
CHILDREN_ACCOUNT_TYPES = ["Children savings", "Children checking"]
YOUTH_ACCOUNT_TYPES = ["Youth savings", "Youth checking"]


def account_type_options_for_age(age):
    if age is not None and age < 14:
        return CHILDREN_ACCOUNT_TYPES
    if age is not None and 14 <= age < 18:
        return YOUTH_ACCOUNT_TYPES
    return ACCOUNT_TYPES




def hash_password(raw_password):
    return hashlib.sha256(raw_password.encode("utf-8")).hexdigest()


def generate_temp_password(length=10):
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def calculate_age(dob_value):
    today = date.today()
    return today.year - dob_value.year - ((today.month, today.day) < (dob_value.month, dob_value.day))


def email_required_for_online_access(age):
    return age >= 14


def format_dk_account(account_id):
    """Display as '8888 · 000xxxxxxx' (registration no. · account no., rest zero-padded to 9)."""
    s = re.sub(r"\s+", "", str(account_id))
    if not s:
        return ""
    if len(s) <= 4:
        return s
    rest = s[4:]
    if rest.isdigit():
        rest = rest.zfill(9)
    return f"{s[:4]} · {rest}"


def format_dkk(value):
    """Danish kr: thousands with . and decimals with , (e.g. 1.234,56 kr)."""
    if value is None:
        return ""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return str(value)
    neg = x < 0
    x = abs(x)
    s = f"{x:.2f}"
    intp, frac = s.split(".")
    parts = []
    while intp:
        parts.insert(0, intp[-3:])
        intp = intp[:-3]
    num = ".".join(parts) + "," + frac
    if neg:
        num = "-" + num
    return f"{num} kr"


def parse_amount_kr(text):
    """Parse amount from text; empty or invalid -> None. Accepts 500, 500.5, 1.234,56 (Danish)."""
    if text is None:
        return None
    s = str(text).strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def normalize_account_digits(account_id):
    """Digits only (no spaces/dashes)."""
    return re.sub(r"\D", "", str(account_id or ""))


def format_ptal_for_input(value):
    # Show p-tal as ddmmyy-xxx while typing.
    d = normalize_account_digits(value)[:9]
    if len(d) <= 6:
        return d
    return f"{d[:6]}-{d[6:]}"


def _format_ptal_key(key):
    st.session_state[key] = format_ptal_for_input(st.session_state.get(key, ""))


def ptal_input(label, key):
    if key not in st.session_state:
        st.session_state[key] = ""
    st.text_input(label, key=key, max_chars=10, on_change=_format_ptal_key, args=(key,))
    return st.session_state.get(key, "")


def is_valid_modulo11(account_id):
    """Check if account number is valid modulo11(5..2 on the first 10 digits, check digit last).
    """
    s = normalize_account_digits(account_id)
    if len(s) != 11:
        return False
    weights = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    tvorsum = sum(int(s[i]) * weights[i] for i in range(10))
    rest = 11 - (tvorsum % 11)
    if rest == 11:
        rest = 0
    elif rest == 10:
        return False
    return int(s[10]) == rest


def lookup_account_id_by_digits(digits):
    """Get account_id from DB or None if not found (digits-only match)."""
    if not digits:
        return None
    rows = fetchall_dict(
        """
        SELECT account_id FROM account
        WHERE REPLACE(REPLACE(TRIM(account_id), ' ', ''), '-', '') = %s
        LIMIT 1
        """,
        (digits,),
    )
    return rows[0]["account_id"] if rows else None


def is_valid_ptal(ptal):
    """Modulo-11 validation for Faroese p-tal (9 digits)."""
    s = normalize_account_digits(ptal)
    if len(s) != 9:
        return False
    weights = [3, 2, 7, 6, 5, 4, 3, 2]
    tvorsum = sum(int(s[i]) * weights[i] for i in range(8))
    rest = 11 - (tvorsum % 11)
    if rest == 11:
        rest = 0
    elif rest == 10:
        return False
    return int(s[8]) == rest


def is_valid_ptal_db(ptal):
    """Ask MySQL is_valid_ptal(), fallback to Python validation if unavailable."""
    s = normalize_account_digits(ptal)
    if len(s) != 9:
        return False
    try:
        rows = fetchall_dict("SELECT is_valid_ptal(%s) AS ok", (s,))
        if not rows:
            return False
        return bool(rows[0].get("ok"))
    except Exception:
        return is_valid_ptal(s)


def ptal_validation_error(ptal, dob_value, gender):
    # Keep this aligned with DB trigger checks.
    s = normalize_account_digits(ptal)
    if len(s) != 9:
        return "P-tal must be exactly 9 digits."
    if dob_value is None:
        return "Date of birth is required for P-tal validation."
    if s[:6] != dob_value.strftime("%d%m%y"):
        return "P-tal must start with date of birth (ddmmyy)."
    g = (gender or "").strip().lower()
    last_digit = int(s[8])
    if g == "male" and last_digit % 2 == 0:
        return "Male must have odd last P-tal digit."
    if g == "female" and last_digit % 2 == 1:
        return "Female must have even last P-tal digit."
    d7 = int(s[6])
    year = dob_value.year
    if 1900 <= year <= 1999 and not (0 <= d7 <= 4):
        return "7th P-tal digit must be 0-4 for birth years 1900-1999."
    if year >= 2000 and not (5 <= d7 <= 9):
        return "7th P-tal digit must be 5-9 for birth years 2000+."
    weights = [3, 2, 7, 6, 5, 4, 3, 2, 1]
    checksum = sum(int(s[i]) * weights[i] for i in range(9))
    if checksum % 11 != 0:
        return "P-tal Modulo-11 check failed."
    return None


def is_valid_email(value):
    """Basic format check: local@domain.tld (ASCII; good enough for coursework)."""
    if value is None:
        return False
    s = str(value).strip()
    if not s or "@" not in s or s.count("@") != 1:
        return False
    local, domain = s.split("@", 1)
    if not local or not domain or "." not in domain:
        return False
    return bool(
        re.fullmatch(
            r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
            s,
        )
    )


def user_facing_db_error(exc):
    # Keep SQL trigger/procedure messages readable in UI.
    msg = str(exc)
    if ":" in msg:
        return msg.split(":")[-1].strip()
    return msg


def _secret_get(mapping, *keys, default=None):
    # Streamlit secret sections behave like dicts but are not always isinstance(..., dict).
    for k in keys:
        try:
            if mapping is None:
                break
            if isinstance(mapping, dict):
                if k in mapping:
                    return mapping[k]
            elif hasattr(mapping, "__getitem__"):
                return mapping[k]
        except (KeyError, TypeError):
            continue
    return default


def _db_params_from_streamlit_secrets():
    # [mysql] / [db] / [database] — Streamlit secrets sections are not always plain dicts.
    if not hasattr(st, "secrets"):
        return None
    try:
        s = st.secrets
        if not s:
            return None
        for section in ("mysql", "db", "database"):
            if section not in s:
                continue
            block = s[section]
            host = _secret_get(block, "host", "hostname", "HOST")
            user = _secret_get(block, "user", "username", "USER")
            password = _secret_get(block, "password", "passwd", "PASSWORD")
            database = _secret_get(block, "database", "db", "schema", "DATABASE")
            port = _secret_get(block, "port", "PORT", default=3306)
            if host and user and database:
                ssl_ca = _secret_get(block, "ssl_ca", "ssl_ca_path")
                params = {
                    "host": host,
                    "port": int(port),
                    "user": user,
                    "password": password if password is not None else "",
                    "database": database,
                }
                if ssl_ca:
                    params["ssl_ca"] = ssl_ca
                return params
        host = _secret_get(s, "MYSQL_HOST", "mysql_host")
        if host:
            user = _secret_get(s, "MYSQL_USER", "mysql_user")
            pwd = _secret_get(s, "MYSQL_PASSWORD", "mysql_password")
            dbn = _secret_get(s, "MYSQL_DATABASE", "mysql_database", default="BANKIN")
            port = _secret_get(s, "MYSQL_PORT", "mysql_port", default=3306)
            if user and dbn:
                return {
                    "host": host,
                    "port": int(port or 3306),
                    "user": user,
                    "password": pwd if pwd is not None else "",
                    "database": dbn,
                }
    except (KeyError, TypeError, AttributeError):
        pass
    return None


def _looks_like_streamlit_cloud():
    return os.path.isdir("/mount/src") or bool(os.environ.get("STREAMLIT_SERVER_PORT"))


def get_db_connection():
    params = _db_params_from_streamlit_secrets()
    if params and params.get("host") and params.get("user") and params.get("database"):
        return mysql.connector.connect(**params)

    env_host = os.environ.get("MYSQL_HOST")
    if env_host:
        return mysql.connector.connect(
            host=env_host,
            port=int(os.environ.get("MYSQL_PORT", "3306")),
            user=os.environ.get("MYSQL_USER", "root"),
            password=os.environ.get("MYSQL_PASSWORD", ""),
            database=os.environ.get("MYSQL_DATABASE", "BANKIN"),
        )

    if _looks_like_streamlit_cloud():
        raise RuntimeError(
            "Database not configured for Streamlit Cloud. In App settings → Secrets, "
            "add a [mysql] section with host, user, password, database (and optional port). "
            "You can also use host / hostname and database / db as key names."
        )

    return mysql.connector.connect(
        host=os.environ.get("MYSQL_HOST", "localhost"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "root"),
        password=os.environ.get("MYSQL_PASSWORD", "12345678"),
        database=os.environ.get("MYSQL_DATABASE", "BANKIN"),
    )


def ensure_support_objects():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT IS_NULLABLE FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'client' AND COLUMN_NAME = 'email'
        """
    )
    row = cursor.fetchone()
    if row and row[0] == "NO":
        cursor.execute("ALTER TABLE client MODIFY email VARCHAR(255) NULL")
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'client' AND COLUMN_NAME = 'p_tal'
        """
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute("ALTER TABLE client ADD COLUMN p_tal VARCHAR(20) NULL")
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'client' AND COLUMN_NAME = 'gender'
        """
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute("ALTER TABLE client ADD COLUMN gender VARCHAR(16) NULL")
    cursor.execute(
        """
        UPDATE client SET gender = 'Male'
        WHERE gender IS NULL OR TRIM(gender) = ''
        """
    )
    cursor.execute("ALTER TABLE client MODIFY gender VARCHAR(16) NOT NULL")
    # Keep p_tal schema aligned with registration requirements.
    cursor.execute(
        """
        UPDATE client
        SET p_tal = LPAD(client_id, 9, '0')
        WHERE p_tal IS NULL OR TRIM(p_tal) = ''
        """
    )
    cursor.execute("ALTER TABLE client MODIFY p_tal VARCHAR(20) NOT NULL")
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'client' AND INDEX_NAME = 'ux_client_p_tal'
        """
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute("ALTER TABLE client ADD UNIQUE INDEX ux_client_p_tal (p_tal)")

    cursor.execute("SELECT DATABASE()")
    _db = cursor.fetchone()[0]
    _drop_ptal_trg = "DROP TRIGGER IF EXISTS before_client_insert_ptal"
    if _db:
        _drop_ptal_trg = (
            "DROP TRIGGER IF EXISTS `"
            + _db.replace("`", "``")
            + "`.`before_client_insert_ptal`"
        )
    cursor.execute(_drop_ptal_trg)
    _create_ptal_trigger = """
        CREATE TRIGGER before_client_insert_ptal
        BEFORE INSERT ON client
        FOR EACH ROW
        BEGIN
            DECLARE v_ptal VARCHAR(20);
            DECLARE v_sum INT;
            DECLARE v_year INT;
            DECLARE v_digit7 INT;
            DECLARE v_digit9 INT;
            DECLARE v_dob6 VARCHAR(6);
            DECLARE v_gender VARCHAR(16);

            SET v_ptal = REGEXP_REPLACE(IFNULL(NEW.p_tal, ''), '[^0-9]', '');
            IF LENGTH(v_ptal) <> 9 THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Invalid p_tal: must be exactly 9 digits.';
            END IF;

            IF NEW.date_of_birth IS NULL THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Invalid client: date_of_birth is required for p_tal validation.';
            END IF;

            SET v_dob6 = DATE_FORMAT(NEW.date_of_birth, '%d%m%y');
            IF SUBSTRING(v_ptal, 1, 6) <> v_dob6 THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Invalid p_tal: first 6 digits must match date of birth (ddmmyy).';
            END IF;

            SET v_gender = LOWER(TRIM(IFNULL(NEW.gender, '')));
            SET v_digit9 = CAST(SUBSTRING(v_ptal, 9, 1) AS UNSIGNED);
            IF v_gender = 'male' AND MOD(v_digit9, 2) = 0 THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Invalid p_tal: male must have odd last digit.';
            END IF;
            IF v_gender = 'female' AND MOD(v_digit9, 2) = 1 THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Invalid p_tal: female must have even last digit.';
            END IF;

            SET v_year = YEAR(NEW.date_of_birth);
            SET v_digit7 = CAST(SUBSTRING(v_ptal, 7, 1) AS UNSIGNED);

            IF v_year BETWEEN 1900 AND 1999 AND v_digit7 NOT BETWEEN 0 AND 4 THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Invalid p_tal: 7th digit must be 0-4 for birth years 1900-1999.';
            END IF;

            IF v_year >= 2000 AND v_digit7 NOT BETWEEN 5 AND 9 THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Invalid p_tal: 7th digit must be 5-9 for birth years 2000+.';
            END IF;

            SET v_sum =
                3 * CAST(SUBSTRING(v_ptal, 1, 1) AS UNSIGNED) +
                2 * CAST(SUBSTRING(v_ptal, 2, 1) AS UNSIGNED) +
                7 * CAST(SUBSTRING(v_ptal, 3, 1) AS UNSIGNED) +
                6 * CAST(SUBSTRING(v_ptal, 4, 1) AS UNSIGNED) +
                5 * CAST(SUBSTRING(v_ptal, 5, 1) AS UNSIGNED) +
                4 * CAST(SUBSTRING(v_ptal, 6, 1) AS UNSIGNED) +
                3 * CAST(SUBSTRING(v_ptal, 7, 1) AS UNSIGNED) +
                2 * CAST(SUBSTRING(v_ptal, 8, 1) AS UNSIGNED) +
                1 * CAST(SUBSTRING(v_ptal, 9, 1) AS UNSIGNED);

            IF MOD(v_sum, 11) <> 0 THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Invalid p_tal: Modulo-11 check failed.';
            END IF;

            SET NEW.p_tal = v_ptal;
        END
    """
    try:
        cursor.execute(_create_ptal_trigger)
    except mysql.connector.Error as e:
        if getattr(e, "errno", None) == 1359:
            cursor.execute(_drop_ptal_trg)
            cursor.execute(_create_ptal_trigger)
        else:
            raise
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS app_user (
            user_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            client_id INT NOT NULL,
            email VARCHAR(255) NOT NULL UNIQUE,
            password_hash VARCHAR(64) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_app_user_client FOREIGN KEY (client_id) REFERENCES client(client_id)
        )
        """
    )
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'account' AND COLUMN_NAME = 'account_type'
        """
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "ALTER TABLE account ADD COLUMN account_type VARCHAR(32) NULL AFTER accountname"
        )
        cursor.execute(
            """
            UPDATE account SET account_type = accountname
            WHERE accountname IN ('Savings', 'Checking', 'Business')
            """
        )
        cursor.execute(
            """
            UPDATE account SET accountname = CONCAT(account_type, ' account')
            WHERE accountname IN ('Savings', 'Checking', 'Business')
            """
        )
        cursor.execute(
            """
            UPDATE account SET
                account_type = TRIM(SUBSTRING_INDEX(SUBSTRING_INDEX(accountname, '(', -1), ')', 1)),
                accountname = TRIM(SUBSTRING_INDEX(accountname, '(', 1))
            WHERE account_type IS NULL
              AND accountname LIKE '%(%)%'
              AND LOCATE('(', accountname) > 0
            """
        )
        cursor.execute(
            """
            UPDATE account SET account_type = 'Savings'
            WHERE account_type IS NULL OR TRIM(account_type) = ''
            """
        )
        cursor.execute(
            """
            UPDATE account SET account_type = 'Savings'
            WHERE account_type NOT IN (
                'Savings', 'Checking', 'Business',
                'Youth savings', 'Youth checking',
                'Children savings', 'Children checking'
            )
            """
        )
        cursor.execute("ALTER TABLE account MODIFY account_type VARCHAR(32) NOT NULL")
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'account'
          AND INDEX_NAME = 'unique_client_account_name'
        """
    )
    if cursor.fetchone()[0] > 0:
        try:
            cursor.execute("ALTER TABLE account DROP INDEX unique_client_account_name")
        except mysql.connector.Error:
            pass

    cursor.execute(
        """
        UPDATE account a
        INNER JOIN account_owner ao ON ao.account_id = a.account_id
        INNER JOIN client c ON c.client_id = ao.client_id
        SET a.account_type = CASE
            WHEN LOWER(a.account_type) LIKE '%check%' THEN 'Youth checking'
            ELSE 'Youth savings'
        END
        WHERE TIMESTAMPDIFF(YEAR, c.date_of_birth, CURDATE()) BETWEEN 14 AND 17
        """
    )
    cursor.execute(
        """
        UPDATE account a
        INNER JOIN account_owner ao ON ao.account_id = a.account_id
        INNER JOIN client c ON c.client_id = ao.client_id
        SET a.account_type = CASE
            WHEN a.account_type = 'Youth checking' THEN 'Checking'
            WHEN a.account_type = 'Youth savings' THEN 'Savings'
            ELSE a.account_type
        END
        WHERE TIMESTAMPDIFF(YEAR, c.date_of_birth, CURDATE()) NOT BETWEEN 14 AND 17
          AND a.account_type IN ('Youth savings', 'Youth checking')
        """
    )

    cursor.execute(
        """
        CREATE OR REPLACE VIEW v_client_balances AS
        SELECT
            c.client_id,
            CONCAT(c.first_name, ' ', c.last_name) AS full_name,
            a.account_id,
            a.accountname,
            a.account_type,
            SUM(t.amount) AS current_balance
        FROM client c
        INNER JOIN account_owner ao ON ao.client_id = c.client_id
        INNER JOIN account a ON a.account_id = ao.account_id
        LEFT JOIN transaction t ON t.account_id = a.account_id
        GROUP BY c.client_id, a.account_id, a.accountname, a.account_type
        """
    )
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'draft' AND COLUMN_NAME = 'description'
        """
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute("ALTER TABLE draft ADD COLUMN description VARCHAR(255) NULL")
    # Older schemas often define status as ENUM('pending','posted').
    # Widen to VARCHAR so approval workflow statuses are valid.
    cursor.execute(
        """
        ALTER TABLE draft
        MODIFY status VARCHAR(32) NOT NULL DEFAULT 'pending'
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS renturokning (
            run_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            fra_dato DATE NULL,
            til_dato DATE NOT NULL,
            sum_renta DECIMAL(15,2) NULL,
            rentuprosent DECIMAL(10,4) NULL,
            debetrenta DECIMAL(15,2) NULL,
            kreditrenta DECIMAL(15,2) NULL,
            prosent DECIMAL(10, 4) NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_renturokning_til_dato (til_dato)
        )
        """
    )
    # Keep older renturokning tables compatible with rokna_rentu inserts.
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'renturokning' AND COLUMN_NAME = 'fra_dato'
        """
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute("ALTER TABLE renturokning ADD COLUMN fra_dato DATE NULL")
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'renturokning' AND COLUMN_NAME = 'sum_renta'
        """
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute("ALTER TABLE renturokning ADD COLUMN sum_renta DECIMAL(15,2) NULL")
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'renturokning' AND COLUMN_NAME = 'rentuprosent'
        """
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute("ALTER TABLE renturokning ADD COLUMN rentuprosent DECIMAL(10,4) NULL")
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'renturokning' AND COLUMN_NAME = 'debetrenta'
        """
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute("ALTER TABLE renturokning ADD COLUMN debetrenta DECIMAL(15,2) NULL")
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'renturokning' AND COLUMN_NAME = 'kreditrenta'
        """
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute("ALTER TABLE renturokning ADD COLUMN kreditrenta DECIMAL(15,2) NULL")
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'renturokning' AND COLUMN_NAME = 'created_at'
        """
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "ALTER TABLE renturokning ADD COLUMN created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP"
        )

    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'transaction' AND COLUMN_NAME = 'transaction_date'
        """
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "ALTER TABLE `transaction` ADD COLUMN transaction_date TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP"
        )

    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'transaction' AND COLUMN_NAME = 'transaction_id'
        """
    )
    if cursor.fetchone()[0] == 0:
        try:
            cursor.execute(
                "ALTER TABLE `transaction` ADD COLUMN transaction_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT UNIQUE KEY"
            )
        except mysql.connector.Error:
            pass

    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'transaction' AND COLUMN_NAME = 'transaction_id'
        """
    )
    _has_transaction_id = cursor.fetchone()[0] > 0
    cursor.execute("DROP VIEW IF EXISTS v_account_statement")
    if _has_transaction_id:
        cursor.execute(
            """
            CREATE OR REPLACE VIEW v_account_statement AS
            SELECT
                t.account_id,
                t.transaction_date,
                t.amount,
                t.description,
                SUM(t.amount) OVER (
                    PARTITION BY t.account_id
                    ORDER BY t.transaction_date, t.transaction_id
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS running_balance
            FROM `transaction` t
            """
        )
    else:
        cursor.execute(
            """
            CREATE OR REPLACE VIEW v_account_statement AS
            SELECT
                t.account_id,
                t.transaction_date,
                t.amount,
                t.description,
                SUM(t.amount) OVER (
                    PARTITION BY t.account_id
                    ORDER BY t.transaction_date, t.amount, t.description
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS running_balance
            FROM `transaction` t
            """
        )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS account_type_config (
            account_type VARCHAR(32) PRIMARY KEY,
            annual_rate DECIMAL(5,2) NOT NULL,
            is_minor_product TINYINT(1) NOT NULL DEFAULT 0,
            description VARCHAR(255) NULL
        )
        """
    )
    cursor.execute(
        """
        INSERT IGNORE INTO account_type_config (account_type, annual_rate, is_minor_product, description) VALUES
        ('Children savings', 4.50, 1, 'Savings for under 14s'),
        ('Children checking', 2.50, 1, 'Checking for under 14s'),
        ('Youth savings', 5.00, 1, 'Savings for 14-17s'),
        ('Youth checking', 3.00, 1, 'Checking for 14-17s'),
        ('Savings', 4.00, 0, 'Regular adult savings'),
        ('Checking', 2.00, 0, 'Regular adult checking'),
        ('Business', 4.00, 0, 'Standard business rate')
        """
    )

    cursor.execute("SELECT DATABASE()")
    _db = cursor.fetchone()[0]
    _drop_trg = "DROP TRIGGER IF EXISTS before_account_owner_insert"
    if _db:
        _drop_trg = (
            "DROP TRIGGER IF EXISTS `"
            + _db.replace("`", "``")
            + "`.`before_account_owner_insert`"
        )
    cursor.execute(_drop_trg)
    _create_trg = """
        CREATE TRIGGER before_account_owner_insert
        BEFORE INSERT ON account_owner
        FOR EACH ROW
        BEGIN
            DECLARE v_age INT;
            DECLARE v_is_minor_prod TINYINT;

            SELECT TIMESTAMPDIFF(YEAR, date_of_birth, CURDATE()) INTO v_age
            FROM client
            WHERE client_id = NEW.client_id;

            SELECT is_minor_product INTO v_is_minor_prod
            FROM account_type_config
            WHERE account_type = (
                SELECT account_type FROM account WHERE account_id = NEW.account_id
            );

            IF v_age >= 18 AND IFNULL(v_is_minor_prod, 0) = 1 THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Business Rule Violation: Adults (18+) cannot be owners of Youth/Children accounts.';
            END IF;
        END
    """
    try:
        cursor.execute(_create_trg)
    except mysql.connector.Error as e:
        if getattr(e, "errno", None) == 1359:
            cursor.execute(_drop_trg)
            cursor.execute(_create_trg)
        else:
            raise

    cursor.execute("DROP PROCEDURE IF EXISTS book_draft_entry")
    cursor.execute(
        """
        CREATE PROCEDURE book_draft_entry(IN p_entry_id INT)
        BEGIN
            DECLARE v_from VARCHAR(20);
            DECLARE v_to VARCHAR(20);
            DECLARE v_amount DECIMAL(15,2);
            DECLARE v_status VARCHAR(20);
            DECLARE v_note VARCHAR(255);

            SELECT from_account_id, to_account_id, amount, status, description
            INTO v_from, v_to, v_amount, v_status, v_note
            FROM draft
            WHERE entry_id = p_entry_id
            FOR UPDATE;

            IF v_status <> 'awaiting_approval' THEN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Draft is not awaiting approval';
            END IF;

            START TRANSACTION;
            IF v_from IS NOT NULL THEN
                INSERT INTO transaction (account_id, amount, description, transaction_date)
                VALUES (
                    v_from,
                    -v_amount,
                    IFNULL(NULLIF(TRIM(v_note), ''), CONCAT('Booked draft #', p_entry_id)),
                    CURRENT_TIMESTAMP
                );
                UPDATE account SET balance = balance - v_amount WHERE account_id = v_from;
            END IF;

            IF v_to IS NOT NULL THEN
                INSERT INTO transaction (account_id, amount, description, transaction_date)
                VALUES (
                    v_to,
                    v_amount,
                    IFNULL(NULLIF(TRIM(v_note), ''), CONCAT('Booked draft #', p_entry_id)),
                    CURRENT_TIMESTAMP
                );
                UPDATE account SET balance = balance + v_amount WHERE account_id = v_to;
            END IF;

            UPDATE draft SET status = 'posted' WHERE entry_id = p_entry_id;
            COMMIT;
        END
        """
    )
    cursor.execute("DROP PROCEDURE IF EXISTS book_all_pending_drafts")
    cursor.execute(
        """
        CREATE PROCEDURE book_all_pending_drafts()
        BEGIN
            DECLARE v_eid INT;
            DECLARE v_continue INT DEFAULT 1;
            WHILE v_continue > 0 DO
                SELECT MIN(entry_id) INTO v_eid FROM draft WHERE status = 'awaiting_approval';
                IF v_eid IS NULL THEN
                    SET v_continue = 0;
                ELSE
                    CALL book_draft_entry(v_eid);
                END IF;
            END WHILE;
        END
        """
    )
    cursor.execute("DROP PROCEDURE IF EXISTS authorize_transaction")
    cursor.execute(
        """
        CREATE PROCEDURE authorize_transaction(IN p_entry_id INT)
        BEGIN
            CALL book_draft_entry(p_entry_id);
        END
        """
    )
    cursor.execute("DROP PROCEDURE IF EXISTS rokna_rentu")
    cursor.execute(
        """
        CREATE PROCEDURE rokna_rentu(IN p_til_dato DATE)
        BEGIN
            DECLARE v_fra DATE;
            DECLARE v_last DATE;
            DECLARE v_acc VARCHAR(64);
            DECLARE v_type VARCHAR(64);
            DECLARE v_rate DECIMAL(10,4);
            DECLARE v_done INT DEFAULT 0;
            DECLARE v_d DATE;
            DECLARE v_bal DECIMAL(18,4);
            DECLARE v_interest DECIMAL(18,4);
            DECLARE v_daily DECIMAL(18,4);
            DECLARE v_total_sum DECIMAL(18,4) DEFAULT 0;
            DECLARE v_total_debet DECIMAL(18,4) DEFAULT 0;
            DECLARE v_total_kredit DECIMAL(18,4) DEFAULT 0;

            DECLARE cur CURSOR FOR SELECT account_id, account_type FROM account;
            DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_done = 1;

            SELECT MAX(til_dato) INTO v_last FROM renturokning;
            IF v_last IS NULL THEN
                SET v_fra = '2026-01-01';
            ELSE
                SET v_fra = DATE_ADD(v_last, INTERVAL 1 DAY);
            END IF;

            IF p_til_dato < v_fra THEN
                SIGNAL SQLSTATE '45000'
                    SET MESSAGE_TEXT = 'Interest end date is before the next allowed window';
            END IF;

            IF EXISTS (SELECT 1 FROM renturokning WHERE til_dato = p_til_dato) THEN
                SIGNAL SQLSTATE '45000'
                    SET MESSAGE_TEXT = 'Interest already recorded for this end date';
            END IF;

            START TRANSACTION;

            OPEN cur;
            read_loop: LOOP
                FETCH cur INTO v_acc, v_type;
                IF v_done THEN
                    LEAVE read_loop;
                END IF;

                SELECT annual_rate INTO v_rate
                FROM account_type_config
                WHERE account_type = TRIM(v_type)
                LIMIT 1;

                IF v_rate IS NULL THEN
                    SET v_rate = 4.0;
                END IF;

                SET v_interest = 0;
                SET v_d = v_fra;
                WHILE v_d <= p_til_dato DO
                    SELECT COALESCE(SUM(amount), 0) INTO v_bal
                    FROM transaction
                    WHERE account_id = v_acc
                      AND (transaction_date IS NULL OR DATE(transaction_date) <= v_d);

                    SET v_daily = v_bal * (v_rate / 100.0) / 365.0;
                    SET v_interest = v_interest + v_daily;
                    SET v_d = DATE_ADD(v_d, INTERVAL 1 DAY);
                END WHILE;

                SET v_interest = ROUND(v_interest, 2);

                IF v_interest <> 0 THEN
                    INSERT INTO transaction (account_id, amount, description, transaction_date)
                    VALUES (
                        v_acc,
                        v_interest,
                        'renta',
                        CAST(CONCAT(p_til_dato, ' 12:00:00') AS DATETIME)
                    );
                    UPDATE account SET balance = balance + v_interest WHERE account_id = v_acc;

                    IF v_interest < 0 THEN
                        SET v_total_debet = v_total_debet + v_interest;
                    ELSE
                        SET v_total_kredit = v_total_kredit + v_interest;
                    END IF;
                    SET v_total_sum = v_total_sum + v_interest;
                END IF;
            END LOOP;

            CLOSE cur;

            INSERT INTO renturokning (fra_dato, til_dato, sum_renta, rentuprosent, debetrenta, kreditrenta)
            VALUES (v_fra, p_til_dato, v_total_sum, NULL, v_total_debet, v_total_kredit);

            COMMIT;
        END
        """
    )

    cursor.execute("DROP PROCEDURE IF EXISTS register_client_account")
    cursor.execute(
        """
        CREATE PROCEDURE register_client_account(
            IN p_first_name VARCHAR(64),
            IN p_last_name VARCHAR(64),
            IN p_email VARCHAR(255),
            IN p_gender VARCHAR(16),
            IN p_ptal VARCHAR(20),
            IN p_dob DATE,
            IN p_accountname VARCHAR(255),
            IN p_account_type VARCHAR(32),
            IN p_allow_login TINYINT,
            IN p_password_hash VARCHAR(64)
        )
        BEGIN
            DECLARE v_client_id INT;
            DECLARE v_account_id VARCHAR(64);
            DECLARE v_age INT;
            DECLARE v_type VARCHAR(32);
            DECLARE v_l VARCHAR(64);

            IF TRIM(IFNULL(p_accountname, '')) = '' THEN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Account name required';
            END IF;
            IF p_allow_login = 1 AND (p_email IS NULL OR TRIM(p_email) = '') THEN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Email required';
            END IF;

            SET v_age = TIMESTAMPDIFF(YEAR, p_dob, CURDATE());
            SET v_l = LOWER(TRIM(IFNULL(p_account_type, '')));

            IF v_age < 14 THEN
                SET v_type = IF(v_l LIKE '%check%', 'Children checking', 'Children savings');
            ELSEIF v_age BETWEEN 14 AND 17 THEN
                SET v_type = IF(v_l LIKE '%check%', 'Youth checking', 'Youth savings');
            ELSE
                IF v_l IN ('youth checking', 'children checking') THEN
                    SET v_type = 'Checking';
                ELSEIF v_l IN ('youth savings', 'children savings') THEN
                    SET v_type = 'Savings';
                ELSE
                    SET v_type = IFNULL(NULLIF(TRIM(p_account_type), ''), 'Savings');
                END IF;
            END IF;

            INSERT INTO client (first_name, last_name, email, gender, p_tal, date_of_birth)
            VALUES (
                p_first_name,
                p_last_name,
                IF(p_allow_login = 1, TRIM(p_email), NULL),
                TRIM(p_gender),
                TRIM(p_ptal),
                p_dob
            );
            SET v_client_id = LAST_INSERT_ID();

            INSERT INTO account (accountname, account_type, balance)
            VALUES (TRIM(p_accountname), v_type, 0);

            SELECT account_id INTO v_account_id
            FROM account
            WHERE accountname = TRIM(p_accountname) AND account_type = v_type
            ORDER BY account_id DESC
            LIMIT 1;

            INSERT INTO account_owner (account_id, client_id) VALUES (v_account_id, v_client_id);

            IF p_allow_login = 1 THEN
                INSERT INTO app_user (client_id, email, password_hash)
                VALUES (v_client_id, TRIM(p_email), p_password_hash);
            END IF;

            SELECT v_client_id AS client_id, v_account_id AS account_id;
        END
        """
    )

    cursor.execute("DROP PROCEDURE IF EXISTS open_account_for_client")
    cursor.execute(
        """
        CREATE PROCEDURE open_account_for_client(
            IN p_client_id INT,
            IN p_accountname VARCHAR(255),
            IN p_account_type VARCHAR(32)
        )
        BEGIN
            DECLARE v_dob DATE;
            DECLARE v_age INT;
            DECLARE v_type VARCHAR(32);
            DECLARE v_l VARCHAR(64);
            DECLARE v_account_id VARCHAR(64);

            IF TRIM(IFNULL(p_accountname, '')) = '' THEN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Account name required';
            END IF;

            SELECT date_of_birth INTO v_dob FROM client WHERE client_id = p_client_id LIMIT 1;

            SET v_age = TIMESTAMPDIFF(YEAR, v_dob, CURDATE());
            SET v_l = LOWER(TRIM(IFNULL(p_account_type, '')));

            IF v_age < 14 THEN
                SET v_type = IF(v_l LIKE '%check%', 'Children checking', 'Children savings');
            ELSEIF v_age BETWEEN 14 AND 17 THEN
                SET v_type = IF(v_l LIKE '%check%', 'Youth checking', 'Youth savings');
            ELSE
                IF v_l IN ('youth checking', 'children checking') THEN
                    SET v_type = 'Checking';
                ELSEIF v_l IN ('youth savings', 'children savings') THEN
                    SET v_type = 'Savings';
                ELSE
                    SET v_type = IFNULL(NULLIF(TRIM(p_account_type), ''), 'Savings');
                END IF;
            END IF;

            INSERT INTO account (accountname, account_type, balance)
            VALUES (TRIM(p_accountname), v_type, 0);

            SELECT account_id INTO v_account_id
            FROM account
            WHERE accountname = TRIM(p_accountname) AND account_type = v_type
            ORDER BY account_id DESC
            LIMIT 1;

            INSERT INTO account_owner (account_id, client_id) VALUES (v_account_id, p_client_id);

            SELECT v_account_id AS account_id, v_type AS account_type;
        END
        """
    )
    conn.commit()
    cursor.close()
    conn.close()


def fetchall_dict(query, params=None):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(query, params or ())
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def fetch_account_type_config():
    """Annual rates and labels from account_type_config (source of truth for interest)."""
    return fetchall_dict(
        """
        SELECT account_type, annual_rate, is_minor_product, description
        FROM account_type_config
        ORDER BY account_type
        """
    )


def execute_sql(query, params=None, callproc=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if callproc:
        cursor.callproc(callproc[0], callproc[1])
    else:
        cursor.execute(query, params or ())
    conn.commit()
    last_id = cursor.lastrowid
    cursor.close()
    conn.close()
    return last_id


def insert_account_with_owner(client_id, accountname, account_type):
    """Call open_account_for_client: DB normalizes account type by owner age and returns ids."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "CALL open_account_for_client(%s, %s, %s)",
            (client_id, accountname, account_type),
        )
        row = cursor.fetchone()
        while cursor.nextset():
            pass
        if not row:
            raise RuntimeError("open_account_for_client returned no row")
        new_acc_id, resolved_type = row[0], row[1]
        conn.commit()
        return new_acc_id, resolved_type
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def upsert_transaction_draft(draft_id, from_acc, to_acc, amount, description=None):
    desc = (description or "").strip() or None
    if draft_id:
        execute_sql(
            "UPDATE draft SET from_account_id=%s, to_account_id=%s, amount=%s, description=%s WHERE entry_id=%s AND status='pending'",
            (from_acc, to_acc, amount, desc, draft_id),
        )
        return draft_id
    return execute_sql(
        "INSERT INTO draft (from_account_id, to_account_id, amount, status, description) VALUES (%s, %s, %s, 'pending', %s)",
        (from_acc, to_acc, amount, desc),
    )


def register_client_cursor(
    cursor, first_name, last_name, email, gender, p_tal, dob, account_name, account_type, allow_login=True
):
    email_clean = (email or "").strip() or None
    gender_clean = (gender or "").strip()
    p_tal_clean = normalize_account_digits(p_tal)
    if allow_login and not email_clean:
        raise ValueError("Email (required).")
    if gender_clean not in ("Male", "Female"):
        raise ValueError("Gender must be Male or Female.")
    if not p_tal_clean:
        raise ValueError("P-tal (required).")
    if not is_valid_ptal_db(p_tal_clean):
        raise ValueError("Invalid P-tal (Modulo-11 / Tvorsum).")
    aname = (account_name or "").strip()
    if not aname:
        raise ValueError("Account name (required).")
    temp_password = None
    pwd_hash = None
    if allow_login:
        temp_password = generate_temp_password()
        pwd_hash = hash_password(temp_password)
    cursor.execute(
        "CALL register_client_account(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            first_name,
            last_name,
            email_clean,
            gender_clean,
            p_tal_clean,
            dob,
            aname,
            account_type,
            1 if allow_login else 0,
            pwd_hash,
        ),
    )
    row = cursor.fetchone()
    while cursor.nextset():
        pass
    if not row:
        raise RuntimeError("register_client_account returned no row")
    client_id = row[0]
    return client_id, temp_password, email_clean


def register_client(first_name, last_name, email, gender, p_tal, dob, account_name, account_type, allow_login=True):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        out = register_client_cursor(
            cursor,
            first_name,
            last_name,
            email,
            gender,
            p_tal,
            dob,
            account_name,
            account_type,
            allow_login=allow_login,
        )
        conn.commit()
        return out
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def create_relationship_cursor(cursor, from_client_id, to_client_id, relation_type):
    cursor.execute(
        "INSERT INTO relationship (from_client_id, to_client_id, relationship_type) VALUES (%s, %s, %s)",
        (from_client_id, to_client_id, relation_type),
    )


def create_relationship(from_client_id, to_client_id, relation_type):
    execute_sql(
        "INSERT INTO relationship (from_client_id, to_client_id, relationship_type) VALUES (%s, %s, %s)",
        (from_client_id, to_client_id, relation_type),
    )


def get_user_client(email, raw_password):
    rows = fetchall_dict(
        "SELECT client_id FROM app_user WHERE email = %s AND password_hash = %s",
        (email, hash_password(raw_password)),
    )
    return rows[0]["client_id"] if rows else None


def is_linked_child(client_id):
    rows = fetchall_dict(
        """
        SELECT relationship_id
        FROM relationship
        WHERE to_client_id = %s AND relationship_type = 'parent'
        LIMIT 1
        """,
        (client_id,),
    )
    return bool(rows)


def get_client_dob(client_id):
    rows = fetchall_dict(
        "SELECT date_of_birth FROM client WHERE client_id = %s",
        (client_id,),
    )
    return rows[0]["date_of_birth"] if rows else None


def child_transaction_access(client_id):
    """
    Returns (is_child, can_deposit, can_transfer_withdraw).
    Under 14: deposit only. Ages 14-17: deposit, transfer, withdraw. Non-children: all.
    """
    if not is_linked_child(client_id):
        return False, True, True
    dob = get_client_dob(client_id)
    if not dob:
        return True, True, True
    age = calculate_age(dob)
    if age < 14:
        return True, True, False
    if age < 18:
        return True, True, True
    return True, True, True


def family_client_ids(client_id):
    ids = {client_id}

    # If this client is registered as a child (has a parent relation pointing to them),
    # they should not see parent accounts.
    if is_linked_child(client_id):
        return sorted(ids)

    spouse_rows = fetchall_dict(
        """
        SELECT to_client_id AS cid
        FROM relationship
        WHERE from_client_id = %s AND relationship_type = 'spouse'
        UNION
        SELECT from_client_id AS cid
        FROM relationship
        WHERE to_client_id = %s AND relationship_type = 'spouse'
        """,
        (client_id, client_id),
    )
    for row in spouse_rows:
        ids.add(row["cid"])

    child_rows = fetchall_dict(
        """
        SELECT to_client_id AS cid
        FROM relationship
        WHERE from_client_id = %s AND relationship_type = 'parent'
        """,
        (client_id,),
    )
    for row in child_rows:
        ids.add(row["cid"])

    return sorted(ids)


def account_ids_owned_by_client(client_id):
    rows = fetchall_dict(
        "SELECT DISTINCT account_id FROM account_owner WHERE client_id = %s ORDER BY account_id",
        (client_id,),
    )
    return [r["account_id"] for r in rows]


def account_owners_with_names(account_id):
    return fetchall_dict(
        """
        SELECT ao.client_id, CONCAT(c.first_name, ' ', c.last_name) AS full_name
        FROM account_owner ao
        JOIN client c ON c.client_id = ao.client_id
        WHERE ao.account_id = %s
        ORDER BY full_name
        """,
        (account_id,),
    )


def family_clients_not_yet_owners(logged_client_id, account_id):
    """Spouse / children (family_client_ids) who are not already on this account."""
    family = set(family_client_ids(logged_client_id))
    owners = {r["client_id"] for r in account_owners_with_names(account_id)}
    missing = sorted(family - owners)
    if not missing:
        return []
    ph = ",".join(["%s"] * len(missing))
    return fetchall_dict(
        f"""
        SELECT client_id, CONCAT(first_name, ' ', last_name) AS full_name
        FROM client
        WHERE client_id IN ({ph})
        ORDER BY full_name
        """,
        tuple(missing),
    )


def next_interest_fra_dato():
    """First day of the next interest window (matches rokna_rentu / renturokning)."""
    try:
        rows = fetchall_dict("SELECT MAX(til_dato) AS last_til_dato FROM renturokning")
        last_til = rows[0]["last_til_dato"] if rows else None
    except Exception:
        last_til = None
    return (last_til + timedelta(days=1)) if last_til else date(2026, 1, 1)


def interest_rows_for_accounts(account_ids):
    """One row per account per interest period; sums all renta postings on the period end date."""
    if not account_ids:
        return []
    ph = ",".join(["%s"] * len(account_ids))
    return fetchall_dict(
        f"""
        SELECT
            r.fra_dato AS period_from,
            r.til_dato AS period_to,
            COALESCE(
                (SELECT annual_rate FROM account_type_config
                 WHERE account_type = a.account_type LIMIT 1),
                4.0
            ) AS annual_rate_pct,
            a.account_type,
            t.account_id,
            SUM(t.amount) AS interest_amount
        FROM (
            SELECT
                til_dato,
                MIN(fra_dato) AS fra_dato
            FROM renturokning
            GROUP BY til_dato
        ) r
        INNER JOIN transaction t
            ON DATE(t.transaction_date) = r.til_dato
            AND (
                LOWER(TRIM(COALESCE(t.description, ''))) = 'renta'
                OR LOWER(TRIM(COALESCE(t.description, ''))) LIKE 'renta %'
                OR LOWER(TRIM(COALESCE(t.description, ''))) LIKE '% renta%'
                OR LOWER(TRIM(COALESCE(t.description, ''))) LIKE '% renta'
            )
        INNER JOIN account a ON a.account_id = t.account_id
        WHERE t.account_id IN ({ph})
        GROUP BY r.fra_dato, r.til_dato, t.account_id, a.account_type
        ORDER BY r.til_dato DESC, t.account_id
        """,
        tuple(account_ids),
    )


def statement_export_period_bounds(label):
    """Returns (date_from, date_to) inclusive, or (None, None) for all time."""
    today = date.today()
    if label == "All time":
        return None, None
    if label == "Past 30 days":
        return today - timedelta(days=30), today
    if label == "Past 60 days":
        return today - timedelta(days=60), today
    if label == "Past 90 days":
        return today - timedelta(days=90), today
    if label == "Past 180 days":
        return today - timedelta(days=180), today
    if label == "Previous calendar month":
        first_this_month = today.replace(day=1)
        last_prev_month = first_this_month - timedelta(days=1)
        first_prev_month = last_prev_month.replace(day=1)
        return first_prev_month, last_prev_month
    return None, None


def _opening_balance_before(account_id, before_date):
    rows = fetchall_dict(
        """
        SELECT COALESCE(SUM(amount), 0) AS s
        FROM transaction
        WHERE account_id = %s AND DATE(transaction_date) < %s
        """,
        (account_id, before_date),
    )
    return float(rows[0]["s"]) if rows else 0.0


def fetch_account_statement_rows(account_id, date_from=None, date_to=None):
    """Running balances from SQL (view or window), not a Python loop."""
    if date_from is not None and date_to is not None:
        return fetchall_dict(
            """
            WITH opening AS (
                SELECT COALESCE(SUM(amount), 0) AS ob
                FROM `transaction`
                WHERE account_id = %s AND DATE(transaction_date) < %s
            ),
            filtered AS (
                SELECT transaction_date, amount, description
                FROM `transaction`
                WHERE account_id = %s AND DATE(transaction_date) BETWEEN %s AND %s
            )
            SELECT
                f.transaction_date,
                f.amount,
                f.description,
                (SELECT ob FROM opening)
                    + SUM(f.amount) OVER (
                        ORDER BY f.transaction_date, f.amount, IFNULL(f.description, '')
                    ) AS running_balance
            FROM filtered f
            """,
            (account_id, date_from, account_id, date_from, date_to),
        )
    return fetchall_dict(
        """
        SELECT transaction_date, amount, description, running_balance
        FROM v_account_statement
        WHERE account_id = %s
        ORDER BY transaction_date, amount, IFNULL(description, '')
        """,
        (account_id,),
    )


def account_statement_csv(account_id, date_from=None, date_to=None):
    rows = fetch_account_statement_rows(account_id, date_from, date_to)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["transaction_date", "amount", "description", "running_balance"])
    for row in rows:
        writer.writerow(
            [
                row["transaction_date"],
                format_dkk(row["amount"]),
                row["description"],
                format_dkk(row["running_balance"]),
            ]
        )
    return output.getvalue()


def _pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def account_statement_pdf(account_id, date_from=None, date_to=None):
    if date_from is not None and date_to is not None:
        opening = _opening_balance_before(account_id, date_from)
        period_line = f"Period: {date_from} to {date_to} (opening balance {format_dkk(opening)})"
    else:
        period_line = "Period: all transactions"
    rows = fetch_account_statement_rows(account_id, date_from, date_to)
    lines = [
        f"Statement for account {format_dk_account(account_id)}",
        period_line,
        "",
    ]
    lines.append("Date | Amount | Description | Running Balance")
    lines.append("-" * 80)
    for row in rows:
        lines.append(
            f"{row['transaction_date']} | {format_dkk(row['amount'])} | {row['description'] or ''} | {format_dkk(row['running_balance'])}"
        )

    y = 800
    content_lines = ["BT", "/F1 10 Tf"]
    for line in lines[:55]:
        content_lines.append(f"1 0 0 1 40 {y} Tm ({_pdf_escape(line)}) Tj")
        y -= 14
    content_lines.append("ET")
    stream = "\n".join(content_lines).encode("latin-1", errors="replace")

    objects = []
    objects.append(b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    objects.append(b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n")
    objects.append(
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 5 0 R /Resources << /Font << /F1 4 0 R >> >> >> endobj\n"
    )
    objects.append(b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")
    objects.append(f"5 0 obj << /Length {len(stream)} >> stream\n".encode("latin-1") + stream + b"\nendstream endobj\n")

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf))
        pdf.extend(obj)
    xref_start = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n".encode("latin-1"))
    pdf.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        pdf.extend(f"{off:010d} 00000 n \n".encode("latin-1"))
    pdf.extend(
        f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode("latin-1")
    )
    return bytes(pdf)


def inject_light_ui_style():
    st.markdown(
        """
        <style>
        /* Light, airy shell */
        [data-testid="stAppViewContainer"],
        [data-testid="stApp"] {
            background: linear-gradient(165deg, #f8fafc 0%, #f1f5f9 45%, #eef2ff 100%);
        }
        [data-testid="stHeader"] {
            background: rgba(255, 255, 255, 0.72);
            backdrop-filter: blur(10px);
            border-bottom: 1px solid rgba(148, 163, 184, 0.22);
        }
        section[data-testid="stSidebar"] {
            background: #ffffff !important;
            border-right: 1px solid #e2e8f0;
        }
        section[data-testid="stSidebar"] .block-container {
            padding-top: 1.25rem;
        }
        .block-container {
            padding-top: 1.25rem !important;
            padding-bottom: 2.5rem !important;
        }
        /* Typography */
        h1, h2, h3 {
            font-weight: 600 !important;
            letter-spacing: -0.02em !important;
            color: #0f172a !important;
        }
        h2, h3 {
            color: #334155 !important;
        }
        /* Inputs & controls */
        .stSelectbox label, .stTextInput label, .stNumberInput label,
        .stDateInput label, .stRadio label, .stCheckbox label {
            font-weight: 500 !important;
            color: #475569 !important;
        }
        div[data-baseweb="input"] > div {
            border-radius: 8px !important;
            border-color: #e2e8f0 !important;
        }
        /* Buttons */
        .stButton > button {
            border-radius: 8px !important;
            font-weight: 500 !important;
            border: none !important;
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
            transition: box-shadow 0.15s ease, transform 0.15s ease;
        }
        .stButton > button:hover {
            box-shadow: 0 4px 14px rgba(37, 99, 235, 0.18);
        }
        /* Expanders */
        .streamlit-expanderHeader {
            border-radius: 8px !important;
            font-weight: 500 !important;
        }
        details[data-testid="stExpander"] {
            border: 1px solid #e2e8f0 !important;
            border-radius: 10px !important;
            background: rgba(255, 255, 255, 0.65) !important;
        }
        /* Data display */
        div[data-testid="stDataFrame"] {
            border: 1px solid #e2e8f0;
            border-radius: 10px;
            overflow: hidden;
        }
        /* Alerts */
        div[data-testid="stNotification"], .stAlert {
            border-radius: 10px !important;
        }
        /* Metrics */
        [data-testid="stMetric"] {
            background: rgba(255, 255, 255, 0.75);
            border: 1px solid #e2e8f0;
            border-radius: 10px;
            padding: 0.75rem 1rem;
        }
        footer { visibility: hidden; height: 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )


st.set_page_config(page_title="BANKIN", layout="wide")
try:
    ensure_support_objects()
except RuntimeError as exc:
    st.error(str(exc))
    st.stop()
except mysql.connector.Error as exc:
    st.error(f"Database connection failed: {user_facing_db_error(exc)}")
    st.stop()
inject_light_ui_style()
st.title("BANKIN")

if "logged_in_client_id" not in st.session_state:
    st.session_state.logged_in_client_id = None
if "dashboard_section" not in st.session_state:
    st.session_state.dashboard_section = "Account Overview"

# Must run before the portal_choice radio: login sets this flag, next run updates state safely.
if st.session_state.pop("_sync_portal_to_customer_after_login", False):
    st.session_state.portal_choice = "Customer"

# Logged-in customers only see the banking app; staff console is available before login.
if st.session_state.logged_in_client_id:
    st.sidebar.caption("Signed in as customer")
    _portal = "Customer"
else:
    st.sidebar.markdown("**Who is signing in?**")
    _portal = st.sidebar.radio(
        "Choose access",
        ["Customer", "Bank staff (operations)"],
        key="portal_choice",
        help="Customers use Register / Log in. Bank staff use the operations console (no customer password here).",
    )


if _portal == "Bank staff (operations)" and not st.session_state.logged_in_client_id:
    tab_auth, tab_interest = st.tabs(["Transaction authorizations", "Interest processing"])

    with tab_auth:
        st.header("Bank Authorization Center")
        to_approve = fetchall_dict(
            """
            SELECT entry_id, from_account_id, to_account_id, amount, description, created_at
            FROM draft
            WHERE status = 'awaiting_approval'
            ORDER BY created_at
            """
        )

        if to_approve:
            st.warning(f"Attention: {len(to_approve)} transactions require manual authorization.")
            st.caption("Tick one or more drafts, then use bulk authorize below.")

            for tx in to_approve:
                with st.expander(f"Authorize Transfer: {format_dkk(tx['amount'])}"):
                    st.checkbox(
                        "Select for bulk authorize",
                        key=f"auth_sel_{tx['entry_id']}",
                    )
                    st.write(
                        f"**From Account:** {format_dk_account(tx['from_account_id']) if tx['from_account_id'] else '—'}"
                    )
                    st.write(
                        f"**To Account:** {format_dk_account(tx['to_account_id']) if tx['to_account_id'] else '—'}"
                    )
                    st.write(f"**Description:** {tx.get('description') or '—'}")
                    st.write(f"**Created:** {tx['created_at']}")

                    c1, c2 = st.columns(2)
                    if c1.button("Authorize & Post", key=f"auth_{tx['entry_id']}"):
                        try:
                            execute_sql("", callproc=("authorize_transaction", [tx["entry_id"]]))
                            st.success("Transaction realised.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Authorization failed: {e}")

                    if c2.button("Reject", key=f"rej_{tx['entry_id']}"):
                        execute_sql(
                            "UPDATE draft SET status = 'rejected' WHERE entry_id = %s AND status = 'awaiting_approval'",
                            (tx["entry_id"],),
                        )
                        st.error("Transaction denied.")
                        st.rerun()

            selected_ids = [
                tx["entry_id"]
                for tx in to_approve
                if st.session_state.get(f"auth_sel_{tx['entry_id']}", False)
            ]
            c_bulk1, c_bulk2 = st.columns([2, 3])
            with c_bulk1:
                bulk_btn = st.button(
                    f"Authorize selected ({len(selected_ids)})",
                    key="auth_bulk_btn",
                    disabled=len(selected_ids) == 0,
                )
            with c_bulk2:
                st.caption("Bulk action posts each selected draft via authorize_transaction.")

            if bulk_btn:
                ok_count = 0
                failed = []
                for entry_id in selected_ids:
                    try:
                        execute_sql("", callproc=("authorize_transaction", [entry_id]))
                        ok_count += 1
                    except Exception as exc:
                        failed.append((entry_id, str(exc)))
                for entry_id in selected_ids:
                    st.session_state.pop(f"auth_sel_{entry_id}", None)
                if ok_count:
                    st.success(f"Authorized {ok_count} draft(s).")
                if failed:
                    st.error(
                        "Some authorizations failed: "
                        + ", ".join([f"#{eid} ({msg})" for eid, msg in failed])
                    )
                st.rerun()
        else:
            st.success("No transactions are currently awaiting approval.")

    with tab_interest:
        st.header("Monthly Interest Processing")
        st.write("Calculate interest for all accounts based on daily balances (bank operations).")
        try:
            _cfg = fetch_account_type_config()
            if _cfg:
                st.dataframe(
                    [
                        {
                            "Account type": r["account_type"],
                            "Annual rate %": float(r["annual_rate"]),
                            "Minor product": bool(r.get("is_minor_product")),
                            "Description": r.get("description") or "",
                        }
                        for r in _cfg
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.warning("No rows in account_type_config.")
        except Exception as exc:
            st.warning(f"Could not load account_type_config: {exc}")

        today = date.today()
        fra_dato = next_interest_fra_dato()
        st.date_input("Calculation Start Date", value=fra_dato, disabled=True, key="admin_interest_fra")

        if fra_dato > today:
            st.warning(
                f"The next interest period starts on {fra_dato.isoformat()}, which is after today. "
                "You cannot run until that period has begun."
            )
            til_dato = st.date_input(
                "Calculation End Date",
                value=today,
                min_value=today,
                max_value=today,
                disabled=True,
                key="admin_interest_til",
            )
        else:
            til_dato = st.date_input(
                "Calculation End Date",
                value=today,
                min_value=fra_dato,
                max_value=today,
                key="admin_interest_til",
            )

        if st.button("Run Interest Processing", help="Posts interest via stored procedure rokna_rentu"):
            if fra_dato > today:
                st.error("Cannot run: the interest window has not started yet.")
            elif til_dato > today:
                st.error("End date cannot be after today.")
            elif til_dato < fra_dato:
                st.error("End date cannot be before start date.")
            else:
                try:
                    execute_sql("", callproc=("rokna_rentu", [til_dato]))
                    st.success(f"Interest distributed from {fra_dato} to {til_dato}!")
                    st.balloons()
                    st.rerun()
                except Exception as e:
                    st.error(f"Interest calculation failed: {e}")

        st.subheader("Interest run history (all accounts)")
        st.caption(
            "Totals per run across the whole bank (one row per completed interest run)."
        )
        try:
            history = fetchall_dict(
                """
                SELECT
                    fra_dato AS period_from,
                    til_dato AS period_to,
                    sum_renta AS total_interest_all_accounts,
                    rentuprosent AS annual_rate_pct,
                    debetrenta,
                    kreditrenta,
                    created_at
                FROM renturokning
                ORDER BY til_dato DESC
                """
            )
            if history:
                hist_display = [
                    {
                        "Period from": r["period_from"],
                        "Period to": r["period_to"],
                        "Total interest (all accounts)": format_dkk(r["total_interest_all_accounts"]),
                        "Annual rate %": (
                            f"{r['annual_rate_pct']:.4g}"
                            if r.get("annual_rate_pct") is not None
                            else "Per account type"
                        ),
                        "Debet renta": format_dkk(r["debetrenta"])
                        if r.get("debetrenta") is not None
                        else "",
                        "Kredit renta": format_dkk(r["kreditrenta"])
                        if r.get("kreditrenta") is not None
                        else "",
                        "Logged at": r.get("created_at"),
                    }
                    for r in history
                ]
                st.dataframe(hist_display, use_container_width=True)
            else:
                st.info("No rows in renturokning yet.")
        except Exception as exc:
            st.warning(f"Could not load interest history: {exc}")

    st.stop()

if st.session_state.logged_in_client_id and st.sidebar.button("Log out"):
    st.session_state.logged_in_client_id = None
    st.session_state.dashboard_section = "Account Overview"
    st.rerun()

auth_action = None
dashboard_section = None
if st.session_state.logged_in_client_id:
    dashboard_section = st.sidebar.radio(
        "Dashboard",
        ["Account Overview", "Transactions", "Interest"],
        key="dashboard_section",
    )
else:
    auth_action = st.sidebar.radio("Account Access", ["Register", "Log in"])

min_dob = date(1900, 1, 1)
max_dob = date.today()

if auth_action == "Register":
    st.header("Register New Customer")
    first_name = st.text_input("First name")
    last_name = st.text_input("Last name")
    gender = st.radio("Gender", ["Male", "Female"], horizontal=True)
    dob = st.date_input(
        "Date of birth",
        value=date(2000, 1, 1),
        min_value=min_dob,
        max_value=max_dob,
        format="DD/MM/YYYY",
    )
    p_tal = ptal_input("P-tal (9 digits)", "ptal_main")
    email = st.text_input("Email")
    account_name = st.text_input(
        "Account name",
        placeholder="e.g. Applicant checking, Rainy day",
        help="Your label for this account.",
    )
    main_age_preview = calculate_age(dob)
    account_type = st.selectbox(
        "Account type",
        account_type_options_for_age(main_age_preview),
    )
    add_spouse = st.checkbox("Add spouse")
    add_children = st.checkbox("Add child")

    spouse_first = spouse_last = spouse_email = ""
    spouse_gender = "Male"
    spouse_p_tal = ""
    spouse_dob = date(2000, 1, 1)
    child_count = 0
    children = []

    if add_spouse:
        st.subheader("Spouse")
        spouse_first = st.text_input("Spouse first name")
        spouse_last = st.text_input("Spouse last name")
        spouse_gender = st.radio("Spouse gender", ["Male", "Female"], horizontal=True)
        spouse_dob = st.date_input(
            "Spouse date of birth",
            value=date(2000, 1, 1),
            min_value=min_dob,
            max_value=max_dob,
            format="DD/MM/YYYY",
        )
        spouse_p_tal = ptal_input("Spouse P-tal (9 digits)", "ptal_spouse")
        spouse_email = st.text_input("Spouse email")
        spouse_account_name = st.text_input(
            "Spouse account name",
            placeholder="e.g. Joint savings",
        )
        spouse_age_preview = calculate_age(spouse_dob)
        spouse_account = st.selectbox(
            "Spouse account type",
            account_type_options_for_age(spouse_age_preview),
        )

    if add_children:
        st.subheader("Children")
        child_count = int(st.number_input("Number of children", min_value=0, max_value=6, step=1))
        for i in range(child_count):
            child_first = st.text_input(f"Child {i + 1} first name")
            child_last = st.text_input(f"Child {i + 1} last name")
            child_gender = st.radio(
                f"Child {i + 1} gender", ["Male", "Female"], horizontal=True, key=f"child_gender_{i}"
            )
            child_dob = st.date_input(
                f"Child {i + 1} date of birth",
                value=date(2015, 1, 1),
                min_value=min_dob,
                max_value=max_dob,
                key=f"child_dob_{i}",
                format="DD/MM/YYYY",
            )
            child_p_tal = ptal_input(f"Child {i + 1} P-tal (9 digits)", f"child_ptal_{i}")
            child_email = st.text_input(f"Child {i + 1} email", key=f"child_email_{i}")
            child_acc_name = st.text_input(
                f"Child {i + 1} account name",
                key=f"child_acc_name_{i}",
                placeholder="e.g. Pocket money",
            )
            child_age_preview = calculate_age(child_dob)
            child_acc = st.selectbox(
                f"Child {i + 1} account type",
                account_type_options_for_age(child_age_preview),
                key=f"child_acc_{i}",
            )
            children.append(
                (child_first, child_last, child_gender, child_dob, child_p_tal, child_email, child_acc_name, child_acc)
            )

    submit = st.button("Create registration")

    if submit:
        if not first_name.strip():
            st.error("First name (required).")
            st.stop()
        if not last_name.strip():
            st.error("Last name (required).")
            st.stop()
        p_tal_digits = normalize_account_digits(p_tal)
        if not p_tal_digits:
            st.error("P-tal (required).")
            st.stop()
        main_ptal_error = ptal_validation_error(p_tal_digits, dob, gender)
        if main_ptal_error:
            st.error(f"Applicant P-tal: {main_ptal_error}")
            st.stop()
        main_age = calculate_age(dob)
        email_trim = email.strip()
        if email_required_for_online_access(main_age) and not email_trim:
            st.error("Applicant email (required).")
            st.stop()
        if email_trim and not is_valid_email(email_trim):
            st.error("Applicant: enter a valid email address.")
            st.stop()
        primary_acc_name = (account_name or "").strip()
        if not primary_acc_name:
            st.error("Account name (required).")
            st.stop()
        main_allow_login = email_required_for_online_access(main_age)
        main_email_db = email_trim if main_allow_login else None

        spouse_id = None
        spouse_payload = None
        if add_spouse:
            if not spouse_first.strip():
                st.error("Spouse first name (required).")
                st.stop()
            if not spouse_last.strip():
                st.error("Spouse last name (required).")
                st.stop()
            spouse_age = calculate_age(spouse_dob)
            spouse_pt = normalize_account_digits(spouse_p_tal)
            if not spouse_pt:
                st.error("Spouse P-tal (required).")
                st.stop()
            spouse_ptal_error = ptal_validation_error(spouse_pt, spouse_dob, spouse_gender)
            if spouse_ptal_error:
                st.error(f"Spouse P-tal: {spouse_ptal_error}")
                st.stop()
            spouse_email_trim = spouse_email.strip()
            if email_required_for_online_access(spouse_age) and not spouse_email_trim:
                st.error("Spouse email (required).")
                st.stop()
            if spouse_email_trim and not is_valid_email(spouse_email_trim):
                st.error("Spouse: enter a valid email address.")
                st.stop()
            spouse_acc_name = (spouse_account_name or "").strip()
            if not spouse_acc_name:
                st.error("Spouse account name (required).")
                st.stop()
            spouse_allow_login = email_required_for_online_access(spouse_age)
            spouse_email_db = spouse_email_trim if spouse_allow_login else None
            spouse_payload = (
                spouse_first,
                spouse_last,
                spouse_email_db,
                spouse_gender,
                spouse_pt,
                spouse_dob,
                spouse_acc_name,
                spouse_account,
                spouse_allow_login,
                spouse_email_trim,
            )

        child_rows_validated = []
        if add_children:
            for idx, (
                child_first,
                child_last,
                child_gender,
                child_dob,
                child_p_tal,
                child_email,
                child_acc_name,
                child_acc,
            ) in enumerate(children, start=1):
                cf = child_first.strip()
                cl = child_last.strip()
                cem = child_email.strip()
                cgender = (child_gender or "").strip()
                cpt = normalize_account_digits(child_p_tal)
                can = (child_acc_name or "").strip()
                if not cf:
                    st.error(f"Child {idx} first name (required).")
                    st.stop()
                if not cl:
                    st.error(f"Child {idx} last name (required).")
                    st.stop()
                if not can:
                    st.error(f"Child {idx} account name (required).")
                    st.stop()
                if cgender not in ("Male", "Female"):
                    st.error(f"Child {idx} gender is required.")
                    st.stop()
                if not cpt:
                    st.error(f"Child {idx} P-tal (required).")
                    st.stop()
                child_ptal_error = ptal_validation_error(cpt, child_dob, cgender)
                if child_ptal_error:
                    st.error(f"Child {idx} P-tal: {child_ptal_error}")
                    st.stop()
                child_age = calculate_age(child_dob)
                if email_required_for_online_access(child_age) and not cem:
                    st.error(f"Child {idx} email (required).")
                    st.stop()
                if cem and not is_valid_email(cem):
                    st.error(f"Child {idx}: enter a valid email address.")
                    st.stop()
                child_allow_login = email_required_for_online_access(child_age)
                child_email_db = cem if child_allow_login else None
                child_rows_validated.append(
                    (cf, cl, child_email_db, cgender, cpt, child_dob, can, child_acc, child_allow_login, cem)
                )

        conn = get_db_connection()
        cursor = conn.cursor()
        conn.autocommit = False
        credentials_to_show = []
        try:
            main_client_id, main_temp_password, _ = register_client_cursor(
                cursor,
                first_name,
                last_name,
                main_email_db,
                gender,
                p_tal_digits,
                dob,
                primary_acc_name,
                account_type,
                allow_login=main_allow_login,
            )
            if main_temp_password:
                credentials_to_show.append(
                    {
                        "role": "Applicant",
                        "login_email": email_trim if main_allow_login else None,
                        "temporary_password": main_temp_password,
                    }
                )

            if spouse_payload:
                (
                    sf,
                    sl,
                    s_email_db,
                    s_gender,
                    s_ptal,
                    s_dob,
                    s_acc_name,
                    s_acc,
                    s_allow,
                    s_email_trim,
                ) = spouse_payload
                spouse_id, spouse_pw, _ = register_client_cursor(
                    cursor, sf, sl, s_email_db, s_gender, s_ptal, s_dob, s_acc_name, s_acc, allow_login=s_allow
                )
                create_relationship_cursor(cursor, main_client_id, spouse_id, "spouse")
                create_relationship_cursor(cursor, spouse_id, main_client_id, "spouse")
                if spouse_pw:
                    credentials_to_show.append(
                        {
                            "role": "Spouse",
                            "login_email": s_email_trim if s_allow else None,
                            "temporary_password": spouse_pw,
                        }
                    )

            for row in child_rows_validated:
                cf, cl, child_email_db, cgender, cpt, child_dob, can, child_acc, child_allow_login, cem = row
                child_id, child_pw, _ = register_client_cursor(
                    cursor,
                    cf,
                    cl,
                    child_email_db,
                    cgender,
                    cpt,
                    child_dob,
                    can,
                    child_acc,
                    allow_login=child_allow_login,
                )
                create_relationship_cursor(cursor, main_client_id, child_id, "parent")
                if spouse_id:
                    create_relationship_cursor(cursor, spouse_id, child_id, "parent")
                if child_pw:
                    credentials_to_show.append(
                        {
                            "role": f"Child ({cf})",
                            "login_email": cem if child_allow_login else None,
                            "temporary_password": child_pw,
                        }
                    )

            conn.commit()
            st.success("Registration completed.")
            if credentials_to_show:
                st.info("Copy these login details now. They are only shown at registration.")
                st.dataframe(credentials_to_show, use_container_width=True)
            else:
                st.info("No online login was created for this registration. Bank accounts were still created.")
        except Exception as exc:
            conn.rollback()
            st.error(f"Registration failed: {user_facing_db_error(exc)}")
        finally:
            cursor.close()
            conn.close()

if auth_action == "Log in":
    st.header("Customer Login")
    with st.form("login"):
        login_email = st.text_input("Email")
        login_password = st.text_input("Password", type="password")
        login_submit = st.form_submit_button("Log in")
    if login_submit:
        le = login_email.strip()
        if not le:
            st.error("Email (required).")
        elif not is_valid_email(le):
            st.error("Enter a valid email address.")
        else:
            client_id = get_user_client(le, login_password)
            if client_id:
                st.session_state.logged_in_client_id = client_id
                st.session_state.dashboard_section = "Account Overview"
                st.session_state._sync_portal_to_customer_after_login = True
                st.success("Logged in successfully.")
                st.rerun()
            else:
                st.error("Invalid credentials.")

if st.session_state.logged_in_client_id:
    st.divider()
    st.subheader(f"Customer Dashboard - {dashboard_section}")
    client_ids = family_client_ids(st.session_state.logged_in_client_id)

    placeholders = ",".join(["%s"] * len(client_ids))
    overview = fetchall_dict(
        f"""
        SELECT
            v.client_id,
            v.full_name,
            v.account_id,
            v.accountname,
            v.account_type,
            v.current_balance,
            COALESCE(cfg.annual_rate, 4.0) AS annual_rate_pct
        FROM v_client_balances v
        INNER JOIN account a ON a.account_id = v.account_id
        LEFT JOIN account_type_config cfg ON cfg.account_type = a.account_type
        WHERE v.client_id IN ({placeholders})
        ORDER BY v.client_id, v.account_id
        """,
        tuple(client_ids),
    )
    account_ids = list(dict.fromkeys(row["account_id"] for row in overview))
    if dashboard_section == "Account Overview":
        overview_display = [
            {
                "client_id": r["client_id"],
                "full_name": r["full_name"],
                "account_id": format_dk_account(r["account_id"]),
                "account_type": r["account_type"],
                "accountname": r["accountname"],
                "annual rate % p.a.": (
                    f"{float(r['annual_rate_pct']):.2f}"
                    if r.get("annual_rate_pct") is not None
                    else "—"
                ),
                "current_balance": format_dkk(
                    r["current_balance"] if r["current_balance"] is not None else 0
                ),
            }
            for r in overview
        ]
        st.dataframe(overview_display, use_container_width=True)

        st.subheader("Open a New Account")
        with st.expander("Open additional account for yourself"):
            self_dob = get_client_dob(st.session_state.logged_in_client_id)
            self_age = calculate_age(self_dob) if self_dob else None
            new_acc_type = st.selectbox(
                "Account type",
                account_type_options_for_age(self_age),
                key="new_acc_type",
            )
            new_acc_name = st.text_input(
                "Account name",
                key="new_acc_name",
                placeholder="e.g. Summer trip, Bills",
            )
            if st.button("Open Account", key="new_acc_open"):
                name = (new_acc_name or "").strip()
                if not name:
                    st.error("Enter an account name.")
                else:
                    try:
                        _new_id, resolved_acc_type = insert_account_with_owner(
                            st.session_state.logged_in_client_id,
                            name,
                            new_acc_type,
                        )
                        st.success(
                            f'New {resolved_acc_type} account "{name}" opened and linked to your profile!'
                        )
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to open account: {e}")

        st.subheader("Account co-owners")
        with st.expander("Manage who owns your accounts (e.g. add your spouse)"):
            st.caption(
                "You can add or remove co-owners for accounts you already own. "
                "Only people linked as family on your profile (spouse, children when you are the parent) can be added."
            )
            owned = account_ids_owned_by_client(st.session_state.logged_in_client_id)
            if not owned:
                st.info("You do not own any accounts yet, so there is nothing to share.")
            else:
                mgr_acc = st.selectbox(
                    "Choose account",
                    owned,
                    format_func=format_dk_account,
                    key="co_own_account",
                )
                owners = account_owners_with_names(mgr_acc)
                st.markdown("**Current owners**")
                for ow in owners:
                    c0, c1 = st.columns([4, 1])
                    with c0:
                        st.write(f"{ow['full_name']} (client #{ow['client_id']})")
                    with c1:
                        if len(owners) < 2:
                            st.caption("—")
                        elif st.button(
                            "Remove",
                            key=f"co_rm_{mgr_acc}_{ow['client_id']}",
                        ):
                            try:
                                execute_sql(
                                    "DELETE FROM account_owner WHERE account_id = %s AND client_id = %s",
                                    (mgr_acc, ow["client_id"]),
                                )
                                st.success("Co-owner removed.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Remove failed: {e}")

                st.markdown("---")
                candidates = family_clients_not_yet_owners(
                    st.session_state.logged_in_client_id, mgr_acc
                )
                if not candidates:
                    st.info("No eligible family members left to add on this account.")
                else:
                    labels = [f"{r['full_name']} (#{r['client_id']})" for r in candidates]
                    pick_idx = st.selectbox(
                        "Family member to add",
                        range(len(labels)),
                        format_func=lambda i: labels[i],
                        key=f"co_add_pick_{mgr_acc}",
                    )
                    if st.button("Add as co-owner", key=f"co_add_btn_{mgr_acc}"):
                        try:
                            new_cid = candidates[pick_idx]["client_id"]
                            execute_sql(
                                "INSERT INTO account_owner (account_id, client_id) VALUES (%s, %s)",
                                (mgr_acc, new_cid),
                            )
                            st.success("Co-owner added.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Add failed: {e}")

    elif dashboard_section == "Transactions":
        if not account_ids:
            st.info("No accounts available for transactions.")
        else:
            _is_child, _can_dep, _can_txn = child_transaction_access(st.session_state.logged_in_client_id)
            if _is_child:
                dob_u = get_client_dob(st.session_state.logged_in_client_id)
                age_u = calculate_age(dob_u) if dob_u else None
                if age_u is not None and age_u < 14:
                    st.info("Accounts under age 14: deposits only. Transfer and withdraw are available from age 14.")
                elif age_u is not None and age_u < 18:
                    st.info("Ages 14–17: you can deposit, transfer, and withdraw on your own accounts.")

            txn_options = ["Deposit", "Transfer", "Withdraw"]
            if _is_child and not _can_txn:
                txn_options = ["Deposit"]

            ph = ",".join(["%s"] * len(account_ids))
            drafts_sql = f"""
                SELECT entry_id, from_account_id, to_account_id, amount, status, created_at, description
                FROM draft
                WHERE status = 'pending'
                AND (
                    from_account_id IN ({ph}) OR to_account_id IN ({ph})
                    OR (from_account_id IS NULL AND to_account_id IN ({ph}))
                    OR (to_account_id IS NULL AND from_account_id IN ({ph}))
                )
                ORDER BY created_at DESC
                """

            if st.session_state.pop("tx_pending_reset", None):
                for k in (
                    "tx_working_draft_id",
                    "tx_w_from",
                    "tx_w_to",
                    "tx_w_type",
                    "tx_amount_text",
                    "tx_w_note",
                    "tx_transfer_mode",
                    "tx_manual_reg",
                    "tx_manual_account",
                ):
                    st.session_state.pop(k, None)
                st.session_state.tx_w_type = txn_options[0]
                st.session_state.tx_w_from = account_ids[0]
                st.session_state.tx_w_to = account_ids[-1] if len(account_ids) > 1 else account_ids[0]
                st.session_state.tx_amount_text = ""
                st.session_state.tx_w_note = ""
                st.session_state.tx_transfer_mode = (
                    "My accounts" if len(account_ids) >= 2 else "Enter account number"
                )
                st.session_state.tx_manual_reg = ""
                st.session_state.tx_manual_account = ""

            if st.session_state.pop("tx_apply_edit", None):
                edit_id = st.session_state.pop("tx_edit_target", None)
                if edit_id:
                    erows = fetchall_dict(
                        "SELECT * FROM draft WHERE entry_id = %s AND status = 'pending'",
                        (edit_id,),
                    )
                    if erows:
                        er = erows[0]
                        fa, ta = er["from_account_id"], er["to_account_id"]
                        if fa and ta:
                            st.session_state.tx_w_type = "Transfer"
                            st.session_state.tx_w_from = fa
                            if ta in account_ids:
                                st.session_state.tx_w_to = ta
                                st.session_state.tx_transfer_mode = "My accounts"
                            else:
                                st.session_state.tx_transfer_mode = "Enter account number"
                                _td = normalize_account_digits(ta)
                                if len(_td) >= 4:
                                    st.session_state.tx_manual_reg = _td[:4]
                                    st.session_state.tx_manual_account = _td[4:]
                                else:
                                    st.session_state.tx_manual_reg = _td
                                    st.session_state.tx_manual_account = ""
                        elif fa:
                            st.session_state.tx_w_type = "Withdraw"
                            st.session_state.tx_w_from = fa
                        else:
                            st.session_state.tx_w_type = "Deposit"
                            st.session_state.tx_w_to = ta
                        st.session_state.tx_amount_text = str(er["amount"])
                        st.session_state.tx_w_note = er.get("description") or ""
                        st.session_state.tx_working_draft_id = edit_id

            if "tx_w_from" not in st.session_state:
                st.session_state.tx_w_from = account_ids[0]
            if "tx_w_to" not in st.session_state:
                st.session_state.tx_w_to = account_ids[-1] if len(account_ids) > 1 else account_ids[0]
            if "tx_w_type" not in st.session_state:
                st.session_state.tx_w_type = txn_options[0]
            if "tx_amount_text" not in st.session_state:
                st.session_state.tx_amount_text = ""
            if "tx_w_note" not in st.session_state:
                st.session_state.tx_w_note = ""
            if "tx_transfer_mode" not in st.session_state:
                st.session_state.tx_transfer_mode = (
                    "My accounts" if len(account_ids) >= 2 else "Enter account number"
                )
            if "tx_manual_reg" not in st.session_state:
                st.session_state.tx_manual_reg = ""
            if "tx_manual_account" not in st.session_state:
                st.session_state.tx_manual_account = ""

            drafts = fetchall_dict(drafts_sql, tuple(account_ids * 4))

            tab_list, tab_create, tab_drafts = st.tabs(
                ["Transactions", "Create Transaction", "Drafts"]
            )

            with tab_list:
                st.subheader("Transaction history")
                hist_acc = st.selectbox(
                    "Choose account",
                    account_ids,
                    format_func=format_dk_account,
                    key="tx_hist_account",
                )
                hist_rows = fetchall_dict(
                    """
                    SELECT transaction_date, amount, description
                    FROM transaction
                    WHERE account_id = %s
                    ORDER BY transaction_date DESC
                    """,
                    (hist_acc,),
                )
                if not hist_rows:
                    st.info("No transactions on this account yet.")
                else:
                    hist_display = [
                        {
                            "Date": r["transaction_date"],
                            "Amount": format_dkk(r["amount"]),
                            "Description": r["description"] or "",
                        }
                        for r in hist_rows
                    ]
                    st.dataframe(hist_display, use_container_width=True)

                    st.markdown("---")
                    st.caption("Statement export")
                    stmt_period = st.selectbox(
                        "Statement period",
                        [
                            "All time",
                            "Past 30 days",
                            "Past 60 days",
                            "Past 90 days",
                            "Past 180 days",
                            "Previous calendar month",
                        ],
                        key="tx_stmt_period",
                    )
                    df_export, dt_export = statement_export_period_bounds(stmt_period)
                    stmt_fmt = st.radio(
                        "Export as",
                        ["CSV", "PDF"],
                        horizontal=True,
                        key="tx_stmt_export_format",
                    )
                    period_slug = (
                        f"{df_export}_{dt_export}"
                        if df_export is not None and dt_export is not None
                        else "all"
                    )
                    if stmt_fmt == "CSV":
                        stmt_data = account_statement_csv(hist_acc, df_export, dt_export)
                        stmt_mime = "text/csv"
                        stmt_name = f"statement_{hist_acc}_{period_slug}.csv"
                    else:
                        stmt_data = account_statement_pdf(hist_acc, df_export, dt_export)
                        stmt_mime = "application/pdf"
                        stmt_name = f"statement_{hist_acc}_{period_slug}.pdf"
                    st.download_button(
                        "Export",
                        data=stmt_data,
                        file_name=stmt_name,
                        mime=stmt_mime,
                        key="tx_stmt_export_download",
                    )

            with tab_create:

                def tx_form_valid(tt, fa, ta, amt):
                    if amt is None or amt <= 0:
                        return False
                    if tt == "Transfer":
                        return bool(
                            fa and ta and normalize_account_digits(fa) != normalize_account_digits(ta)
                        )
                    if tt == "Deposit":
                        return bool(ta)
                    return bool(fa)

                txn_type = st.selectbox("Type", txn_options, key="tx_w_type")

                from_acc = None
                to_acc = None
                if txn_type == "Transfer":
                    if not account_ids:
                        st.warning("No accounts available.")
                    else:
                        if len(account_ids) < 2:
                            st.session_state.tx_transfer_mode = "Enter account number"
                        from_acc = st.selectbox(
                            "From account",
                            account_ids,
                            format_func=format_dk_account,
                            key="tx_w_from",
                        )
                        if len(account_ids) >= 2:
                            transfer_mode = st.radio(
                                "Transfer to",
                                ["My accounts", "Enter account number"],
                                horizontal=True,
                                key="tx_transfer_mode",
                            )
                        else:
                            transfer_mode = "Enter account number"
                        if transfer_mode == "My accounts":
                            to_acc = st.selectbox(
                                "To account",
                                account_ids,
                                format_func=format_dk_account,
                                key="tx_w_to",
                            )
                        else:
                            col_reg, col_acc = st.columns([1, 2])
                            with col_reg:
                                st.text_input(
                                    "Registration no.",
                                    key="tx_manual_reg",
                                    max_chars=4,
                                )
                            with col_acc:
                                st.text_input(
                                    "Account no.",
                                    key="tx_manual_account",
                                    max_chars=7,
                                )
                            reg_d = normalize_account_digits(
                                st.session_state.get("tx_manual_reg", "")
                            )
                            acc_d = normalize_account_digits(
                                st.session_state.get("tx_manual_account", "")
                            )
                            digits = reg_d + acc_d
                            if reg_d or acc_d:
                                if len(digits) != 11:
                                    st.info(
                                        "Enter all **4** digits in Registration no. and **7** in Account no."
                                    )
                                elif not is_valid_modulo11(digits):
                                    st.error(
                                        "Account number does not pass Modulo-11 (Tvørsum)."
                                    )
                                else:
                                    found = lookup_account_id_by_digits(digits)
                                    if found:
                                        to_acc = found
                                        st.success(
                                            "Valid number — destination found: "
                                            f"{format_dk_account(found)}"
                                        )
                                    else:
                                        st.error(
                                            "Tvørsum OK, but no account with that number exists "
                                            "in this bank."
                                        )
                elif txn_type == "Deposit":
                    to_acc = st.selectbox(
                        "Deposit to", account_ids, format_func=format_dk_account, key="tx_w_to"
                    )
                else:
                    from_acc = st.selectbox(
                        "Withdraw from", account_ids, format_func=format_dk_account, key="tx_w_from"
                    )

                st.text_input(
                    "Amount (kr)",
                    key="tx_amount_text",
                    help="Danish format: thousands with . and decimals with ,",
                )
                amount = parse_amount_kr(st.session_state.get("tx_amount_text", ""))
                st.text_input(
                    "Note (optional)",
                    key="tx_w_note",
                    placeholder="Description for the transaction",
                )
                note_val = (st.session_state.get("tx_w_note") or "").strip() or None

                if st.session_state.pop("tx_flash_saved", None):
                    st.success("Saved as draft.")
                if st.session_state.pop("tx_flash_submitted", None):
                    st.success("Transaction submitted for approval.")

                colx, coly = st.columns(2)
                with colx:
                    save_manual = st.button("Save as draft")
                with coly:
                    submit_tx = st.button("Submit for approval")

                def sync_draft():
                    wid = st.session_state.get("tx_working_draft_id")
                    new_wid = upsert_transaction_draft(
                        wid, from_acc, to_acc, amount, description=note_val
                    )
                    if not wid:
                        st.session_state.tx_working_draft_id = new_wid
                    return st.session_state.tx_working_draft_id

                ok = tx_form_valid(txn_type, from_acc, to_acc, amount)

                if submit_tx and ok:
                    try:
                        did = sync_draft()
                        execute_sql("UPDATE draft SET status = 'awaiting_approval' WHERE entry_id = %s AND status = 'pending'", (did,))
                        st.session_state.tx_pending_reset = True
                        st.session_state.tx_flash_submitted = True
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Submit failed: {exc}")
                elif submit_tx and not ok:
                    st.error("Complete the form before submitting.")

                elif save_manual and ok:
                    sync_draft()
                    st.session_state.tx_pending_reset = True
                    st.session_state.tx_flash_saved = True
                    st.rerun()
                elif save_manual and not ok:
                    st.error("Complete the form before saving a draft.")

                elif ok and not submit_tx and not save_manual:
                    sync_draft()

            with tab_drafts:
                if not drafts:
                    st.info("No pending drafts.")
                else:
                    for row in drafts:
                        c0, c1, c2, c3 = st.columns([4, 1, 1, 1])
                        with c0:
                            fa = format_dk_account(row["from_account_id"]) if row["from_account_id"] else "—"
                            ta = format_dk_account(row["to_account_id"]) if row["to_account_id"] else "—"
                            note_d = row.get("description") or ""
                            note_html = f" &nbsp; _{note_d}_" if note_d else ""
                            st.markdown(
                                f"**#{row['entry_id']}** &nbsp; {fa} → {ta} &nbsp; "
                                f"**{format_dkk(row['amount'])}** &nbsp; `{row['created_at']}`{note_html}"
                            )
                        with c1:
                            if st.button("Edit", key=f"tx_ed_{row['entry_id']}"):
                                st.session_state.tx_apply_edit = True
                                st.session_state.tx_edit_target = row["entry_id"]
                                st.rerun()
                        with c2:
                            if st.button("Delete", key=f"tx_del_{row['entry_id']}"):
                                execute_sql(
                                    "DELETE FROM draft WHERE entry_id = %s AND status = 'pending'",
                                    (row["entry_id"],),
                                )
                                if st.session_state.get("tx_working_draft_id") == row["entry_id"]:
                                    st.session_state.pop("tx_working_draft_id", None)
                                st.rerun()
                        with c3:
                            if st.button("Submit", key=f"tx_sub_{row['entry_id']}"):
                                try:
                                    execute_sql("UPDATE draft SET status = 'awaiting_approval' WHERE entry_id = %s AND status = 'pending'", (row["entry_id"],))
                                    if st.session_state.get("tx_working_draft_id") == row["entry_id"]:
                                        st.session_state.pop("tx_working_draft_id", None)
                                    st.success("Transaction submitted for approval.")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(f"Submit failed: {exc}")

    elif dashboard_section == "Interest":
        st.header("Monthly Interest Calculations")

        if not account_ids:
            st.info("No accounts available.")
        else:
            try:
                rows = interest_rows_for_accounts(account_ids)
                if rows:
                    by_period = [
                        {
                            "Period from": r["period_from"],
                            "Period to": r["period_to"],
                            "Annual rate % p.a.": (
                                f"{float(r['annual_rate_pct']):.2f}"
                                if r.get("annual_rate_pct") is not None
                                else "—"
                            ),
                            "Account": format_dk_account(r["account_id"]),
                            "Account type": r.get("account_type") or "",
                            "Interest": format_dkk(r["interest_amount"]),
                        }
                        for r in rows
                    ]
                    st.dataframe(by_period, use_container_width=True)
                else:
                    st.info(
                        "No interest postings found for your accounts yet. "
                        "The bank-wide run history is one total per completed run; it does not list each "
                        "customer. Your family’s accounts show here only after staff run interest and "
                        "credit **renta** transactions for your account IDs on the run end date. "
                        "If you registered after the last run, or the next run hasn’t happened yet, "
                        "wait for the next interest period."
                    )
            except Exception as exc:
                st.warning(f"Could not load interest breakdown: {exc}")
