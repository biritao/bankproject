import csv
import hashlib
import io
import os
import random
import smtplib
import string
import uuid
from datetime import date
from email.message import EmailMessage

import mysql.connector
import streamlit as st


def get_db_connection():
    return mysql.connector.connect(
        host="localhost",
        port=3306,
        user="root",
        password="12345678",
        database="BANKIN",
    )


def hash_password(raw_password):
    return hashlib.sha256(raw_password.encode("utf-8")).hexdigest()


def generate_temp_password(length=10):
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def calculate_age(dob_value):
    today = date.today()
    return today.year - dob_value.year - ((today.month, today.day) < (dob_value.month, dob_value.day))


def generate_internal_email(prefix="child"):
    return f"{prefix}.{uuid.uuid4().hex[:12]}@bankin.local"


def send_login_email(recipient, temp_password):
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM", smtp_user if smtp_user else "no-reply@bankin.local")

    if not smtp_host or not smtp_user or not smtp_password:
        return False, "SMTP not configured. Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD."

    msg = EmailMessage()
    msg["Subject"] = "Your BANKIN login details"
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(
        "Welcome to BANKIN.\n\n"
        f"Username (email): {recipient}\n"
        f"Temporary password: {temp_password}\n\n"
        "Please log in and change your password later."
    )
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return True, "Email sent."
    except Exception as exc:
        return False, f"Email sending failed: {exc}"


def ensure_support_objects():
    conn = get_db_connection()
    cursor = conn.cursor()
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
    cursor.execute("SHOW PROCEDURE STATUS WHERE Db = 'BANKIN' AND Name = 'book_draft_entry'")
    exists = cursor.fetchone()
    if not exists:
        cursor.execute("DROP PROCEDURE IF EXISTS book_draft_entry")
        cursor.execute(
            """
            CREATE PROCEDURE book_draft_entry(IN p_entry_id INT)
            BEGIN
                DECLARE v_from VARCHAR(20);
                DECLARE v_to VARCHAR(20);
                DECLARE v_amount DECIMAL(15,2);
                DECLARE v_status VARCHAR(20);

                SELECT from_account_id, to_account_id, amount, status
                INTO v_from, v_to, v_amount, v_status
                FROM draft
                WHERE entry_id = p_entry_id
                FOR UPDATE;

                IF v_status <> 'pending' THEN
                    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Draft already processed';
                END IF;

                START TRANSACTION;
                IF v_from IS NOT NULL THEN
                    INSERT INTO transaction (account_id, amount, description)
                    VALUES (v_from, -v_amount, CONCAT('Booked draft #', p_entry_id));
                END IF;

                IF v_to IS NOT NULL THEN
                    INSERT INTO transaction (account_id, amount, description)
                    VALUES (v_to, v_amount, CONCAT('Booked draft #', p_entry_id));
                END IF;

                UPDATE draft SET status = 'posted' WHERE entry_id = p_entry_id;
                COMMIT;
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


def register_client(first_name, last_name, email, dob, account_type, allow_login=True):
    conn = get_db_connection()
    cursor = conn.cursor()
    stored_email = email if email else generate_internal_email("child")
    cursor.execute(
        "INSERT INTO client (first_name, last_name, email, date_of_birth) VALUES (%s, %s, %s, %s)",
        (first_name, last_name, stored_email, dob),
    )
    client_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO account (client_id, accountname, balance) VALUES (%s, %s, 0)",
        (client_id, account_type),
    )
    temp_password = None
    if allow_login:
        temp_password = generate_temp_password()
        cursor.execute(
            "INSERT INTO app_user (client_id, email, password_hash) VALUES (%s, %s, %s)",
            (client_id, stored_email, hash_password(temp_password)),
        )
    conn.commit()
    cursor.close()
    conn.close()
    return client_id, temp_password, stored_email


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


def account_statement_csv(account_id):
    rows = fetchall_dict(
        """
        SELECT transaction_date, amount, description
        FROM transaction
        WHERE account_id = %s
        ORDER BY transaction_date
        """,
        (account_id,),
    )
    running = 0
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["transaction_date", "amount", "description", "running_balance"])
    for row in rows:
        running += float(row["amount"])
        writer.writerow([row["transaction_date"], row["amount"], row["description"], f"{running:.2f}"])
    return output.getvalue()


def _pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def account_statement_pdf(account_id):
    rows = fetchall_dict(
        """
        SELECT transaction_date, amount, description
        FROM transaction
        WHERE account_id = %s
        ORDER BY transaction_date
        """,
        (account_id,),
    )
    running = 0.0
    lines = [f"Statement for account {account_id}", ""]
    lines.append("Date | Amount | Description | Running Balance")
    lines.append("-" * 80)
    for row in rows:
        running += float(row["amount"])
        lines.append(
            f"{row['transaction_date']} | {row['amount']} | {row['description'] or ''} | {running:.2f}"
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


ensure_support_objects()
st.set_page_config(page_title="BANKIN", layout="wide")
st.title("BANKIN - Registration & Family Banking")

if "logged_in_client_id" not in st.session_state:
    st.session_state.logged_in_client_id = None
if "dashboard_section" not in st.session_state:
    st.session_state.dashboard_section = "Account Overview"

if st.session_state.logged_in_client_id and st.sidebar.button("Log out"):
    st.session_state.logged_in_client_id = None
    st.session_state.dashboard_section = "Account Overview"
    st.rerun()

auth_action = None
dashboard_section = None
if st.session_state.logged_in_client_id:
    dashboard_section = st.sidebar.radio(
        "Dashboard",
        ["Account Overview", "Transactions", "Interest", "Statements"],
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
    email = st.text_input("Email")
    dob = st.date_input("Date of birth", value=date(2000, 1, 1), min_value=min_dob, max_value=max_dob)
    account_type = st.selectbox("Account type", ["Savings", "Checking", "Business"])
    add_spouse = st.checkbox("Add spouse")
    add_children = st.checkbox("Add child")

    spouse_first = spouse_last = spouse_email = ""
    spouse_dob = date(2000, 1, 1)
    spouse_account = "Checking"
    child_count = 0
    children = []

    if add_spouse:
        st.subheader("Spouse")
        spouse_first = st.text_input("Spouse first name")
        spouse_last = st.text_input("Spouse last name")
        spouse_email = st.text_input("Spouse email")
        spouse_dob = st.date_input(
            "Spouse date of birth",
            value=date(2000, 1, 1),
            min_value=min_dob,
            max_value=max_dob,
        )
        spouse_account = st.selectbox("Spouse account type", ["Savings", "Checking", "Business"])

    if add_children:
        st.subheader("Children")
        child_count = int(st.number_input("Number of children", min_value=0, max_value=6, step=1))
        for i in range(child_count):
            child_first = st.text_input(f"Child {i + 1} first name")
            child_last = st.text_input(f"Child {i + 1} last name")
            child_email = st.text_input(f"Child {i + 1} email (optional if under 18)")
            child_dob = st.date_input(
                f"Child {i + 1} date of birth",
                value=date(2015, 1, 1),
                min_value=min_dob,
                max_value=max_dob,
                key=f"child_dob_{i}",
            )
            child_acc = st.selectbox(
                f"Child {i + 1} account type",
                ["Savings", "Checking"],
                key=f"child_acc_{i}",
            )
            children.append((child_first, child_last, child_email, child_dob, child_acc))

    submit = st.button("Create registration")

    if submit:
        try:
            if not first_name or not last_name:
                st.error("Main customer first name and last name are required.")
                st.stop()
            if calculate_age(dob) >= 18 and not email:
                st.error("Main customer email is required for adults.")
                st.stop()

            main_client_id, main_temp_password, main_stored_email = register_client(
                first_name, last_name, email, dob, account_type, allow_login=True
            )
            credentials_to_show = [
                {
                    "role": "Primary",
                    "login_email": email or main_stored_email,
                    "temporary_password": main_temp_password,
                }
            ]
            spouse_id = None

            if add_spouse:
                if not spouse_first or not spouse_last:
                    st.error("Spouse first name and last name are required when spouse is enabled.")
                    st.stop()
                if calculate_age(spouse_dob) >= 18 and not spouse_email:
                    st.error("Spouse email is required for adults.")
                    st.stop()
                spouse_id, spouse_pw, spouse_stored_email = register_client(
                    spouse_first, spouse_last, spouse_email, spouse_dob, spouse_account, allow_login=True
                )
                create_relationship(main_client_id, spouse_id, "spouse")
                create_relationship(spouse_id, main_client_id, "spouse")
                credentials_to_show.append(
                    {
                        "role": "Spouse",
                        "login_email": spouse_email or spouse_stored_email,
                        "temporary_password": spouse_pw,
                    }
                )

            created_children = 0
            if add_children:
                for child_first, child_last, child_email, child_dob, child_acc in children:
                    child_age = calculate_age(child_dob)
                    requires_email = child_age >= 18
                    if child_first and child_last and (child_email or not requires_email):
                        child_id, child_pw, child_stored_email = register_client(
                            child_first,
                            child_last,
                            child_email,
                            child_dob,
                            child_acc,
                            allow_login=True,
                        )
                        create_relationship(main_client_id, child_id, "parent")
                        if spouse_id:
                            create_relationship(spouse_id, child_id, "parent")
                        created_children += 1
                        credentials_to_show.append(
                            {
                                "role": f"Child ({child_first})",
                                "login_email": child_email or child_stored_email,
                                "temporary_password": child_pw,
                            }
                        )
                    elif child_first or child_last or child_email:
                        st.warning(
                            "Skipped one child entry because required fields were missing (email is required for 18+)."
                        )

            if add_children and child_count > 0 and created_children == 0:
                st.warning(
                    "No child was created. Fill first/last name for all children and email for any child aged 18+."
                )
            st.success("Registration completed.")
            st.info("Copy these credentials now. They are shown only at creation time.")
            st.dataframe(credentials_to_show, use_container_width=True)
        except Exception as exc:
            st.error(f"Registration failed: {exc}")

if auth_action == "Log in":
    st.header("Customer Login")
    st.caption("Use the login email shown at registration (for children without their own email, use the bank-generated address).")
    with st.form("login"):
        login_email = st.text_input("Login email")
        login_password = st.text_input("Password", type="password")
        login_submit = st.form_submit_button("Log in")
    if login_submit:
        client_id = get_user_client(login_email, login_password)
        if client_id:
            st.session_state.logged_in_client_id = client_id
            st.session_state.dashboard_section = "Account Overview"
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
        SELECT c.client_id, c.first_name, c.last_name, a.account_id, a.accountname,
               IFNULL(SUM(t.amount), 0) AS balance
        FROM client c
        JOIN account a ON a.client_id = c.client_id
        LEFT JOIN transaction t ON t.account_id = a.account_id
        WHERE c.client_id IN ({placeholders})
        GROUP BY c.client_id, c.first_name, c.last_name, a.account_id, a.accountname
        ORDER BY c.client_id, a.account_id
        """,
        tuple(client_ids),
    )
    account_ids = [row["account_id"] for row in overview]
    if dashboard_section == "Account Overview":
        st.dataframe(overview, use_container_width=True)

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

            st.subheader("Draft Transactions")
            txn_options = ["Deposit", "Transfer", "Withdraw"]
            if _is_child and not _can_txn:
                txn_options = ["Deposit"]
            txn_type = st.selectbox("Type", txn_options)
            from_acc = None
            to_acc = None
            amount = st.number_input("Amount", min_value=0.01, step=100.0)

            if txn_type == "Transfer":
                from_acc = st.selectbox("From account", account_ids, key="tx_from")
                to_acc = st.selectbox("To account", account_ids, key="tx_to")
            elif txn_type == "Deposit":
                to_acc = st.selectbox("Deposit to", account_ids, key="tx_to_dep")
            else:
                from_acc = st.selectbox("Withdraw from", account_ids, key="tx_from_wd")

            if st.button("Create draft"):
                try:
                    draft_id = execute_sql(
                        "INSERT INTO draft (from_account_id, to_account_id, amount, status) VALUES (%s, %s, %s, 'pending')",
                        (from_acc, to_acc, amount),
                    )
                    st.success(f"Draft #{draft_id} created.")
                except Exception as exc:
                    st.error(f"Could not create draft: {exc}")

            ph = ",".join(["%s"] * len(account_ids))
            drafts = fetchall_dict(
                f"""
                SELECT entry_id, from_account_id, to_account_id, amount, status, created_at
                FROM draft
                WHERE status = 'pending'
                AND (
                    from_account_id IN ({ph}) OR to_account_id IN ({ph})
                    OR (from_account_id IS NULL AND to_account_id IN ({ph}))
                    OR (to_account_id IS NULL AND from_account_id IN ({ph}))
                )
                ORDER BY created_at DESC
                """,
                tuple(account_ids * 4),
            )
            if drafts:
                st.dataframe(drafts, use_container_width=True)
                draft_ids = [row["entry_id"] for row in drafts]
                selected_draft = st.selectbox("Draft to book", draft_ids)
                if st.button("Book selected draft"):
                    try:
                        execute_sql("", callproc=("book_draft_entry", [selected_draft]))
                        st.success("Draft booked successfully.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Booking failed: {exc}")
            else:
                st.info("No pending drafts.")

    elif dashboard_section == "Interest":
        st.subheader("Interest Calculation")
        col1, col2 = st.columns(2)
        start = col1.date_input("Start date", date(2026, 3, 1))
        end = col2.date_input("End date", date(2026, 3, 31))
        rate = st.number_input("Interest rate", value=0.02, step=0.01, format="%.4f")
        if st.button("Run interest procedure"):
            try:
                execute_sql("", callproc=("rokna_rentu", [start, end, rate]))
                st.success("Interest procedure executed.")
            except Exception as exc:
                st.error(f"Interest calculation failed: {exc}")

    elif dashboard_section == "Statements":
        st.subheader("Account Statement Download")
        if not account_ids:
            st.info("No accounts available for statement download.")
        else:
            statement_account = st.selectbox("Select account", account_ids, key="statement_acc")
            statement_csv = account_statement_csv(statement_account)
            statement_pdf = account_statement_pdf(statement_account)
            st.download_button(
                label="Download CSV statement",
                data=statement_csv,
                file_name=f"statement_{statement_account}.csv",
                mime="text/csv",
            )
            st.download_button(
                label="Download PDF statement",
                data=statement_pdf,
                file_name=f"statement_{statement_account}.pdf",
                mime="application/pdf",
            )