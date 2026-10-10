# Put the sales service on Render

The code for this service is in the `service` folder. Render deploys from GitHub. The service is not on GitHub until you confirm a push. Do these steps after that push.

Use the same Render account that hosts the Zendesk agents. This is a second web service. Do not change the Zendesk service.

## 1. Google service account

The sheet is [SR Event Sales](https://docs.google.com/spreadsheets/d/1VWqP7raa6taFi9JCSN8NUXpVooPBM_6xuEF_IEVK-0E/edit).

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project, or pick an existing one. Name it something like `sr-event-sales`.
3. APIs & Services → Library → enable **Google Sheets API**.
4. APIs & Services → Credentials → Create credentials → Service account. Name it `event-sales`.
5. Open that service account → Keys → Add key → JSON. A file downloads. Keep it private.
6. Open the JSON and copy the `client_email` value. It looks like `event-sales@....iam.gserviceaccount.com`.
7. Open the SR Event Sales sheet → Share → paste that email → Editor → Send.

When the service starts, it renames the first tab from `SR Event Sales` to `MMI2610ROT` (Rotterdam, October 2026).

## 2. Create the Render service

1. Log in to [Render](https://dashboard.render.com/) with the same account as the Zendesk agents.
2. New → Web Service.
3. Connect the repository `Success-Resources/Sales-Automation`. Branch `main`.
4. Name: `sr-event-sales`.
5. Root Directory: `service`.
6. Runtime: Python 3.
7. Build command: `pip install -r requirements.txt`
8. Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
9. Instance type: the smallest paid or free instance you already use. A webhook must stay awake, so a free instance that sleeps will miss payments or answer Stripe late. Prefer the same always-on type as the Zendesk service.
10. Add these environment variables before the first deploy finishes:

| Key | Value |
| --- | --- |
| `STRIPE_SECRET_KEY` | Stripe secret key, test mode first (`sk_test_...`) |
| `STRIPE_WEBHOOK_SECRET` | Leave blank until step 4, then paste the signing secret (`whsec_...`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The entire JSON file, as one value |
| `SALES_SHEET_ID` | `1VWqP7raa6taFi9JCSN8NUXpVooPBM_6xuEF_IEVK-0E` |

11. Create the web service. Wait until it is live.
12. Open `https://<your-service>.onrender.com/health`. You want `"ok": true` and `"sheet_configured": true`. `stripe_configured` stays false until both Stripe values are set.

## 3. Stripe products for the Rotterdam test

In Stripe, turn **Test mode** on. Create one product for the test, named exactly:

`MMI2610ROT | Speed Wealth Book`

Add an installment price, for example 10.00 EUR. Create a Payment Link for that price. The QR code later is that link. For the test, open the link in a browser.

A one-time full payment is a second price on the same product. That payment is recorded only when the destination also receives `checkout.session.completed`. On this Stripe account that event is not in the list you can select, so the first test is the installment price.

The other Rotterdam products, when you are ready, use the same left side and a different programme:

- `MMI2610ROT | Quantum Leap 2027`
- `MMI2610ROT | Enlightened Warrior Camp 2027`
- `MMI2610ROT | Train the Trainer 2027`
- `MMI2610ROT | Guerrilla Business Intensive 2027`
- `MMI2610ROT | Never Work Again 2027`
- `MMI2610ROT | Speed Wealth Book`

A full payment and an installment plan are two prices on that product. Each price has its own Payment Link.

## 4. Stripe event destination

Stripe does not show **Add endpoint**. The control is **Create an event destination** (Workbench → Webhooks). Stay in **Test mode**.

1. Event destination scope: **Your account**.
2. Payload style: **Snapshot**. Do not choose Thin. Thin shows an Accounts v2 group, and that group is not these payment events.
3. API version: **2026-08-26.dahlia**. Do not choose `2026-08-26.preview`, or the 2018 and 2019 versions.
4. Events. The screen opens on the **Subscriptions** group. That group includes `invoice.paid`. Select it.
   - `checkout.session.completed` is in the **Checkout** group. It records a one-time full payment. Installments are not taken from this event.
   - `charge.refunded` is in the **Charge** group. It does not add a row. It marks an existing row `refunded`.
   - Developer access is enough to see those groups. On this account the list still shows only Subscriptions, so select `invoice.paid` and continue. The account owner can add the other two events on this same destination later.
5. Destination type: **Webhook endpoint**.
6. Endpoint URL: `https://<your-service>.onrender.com/stripe/webhook`
7. Create the destination. Open it and reveal the signing secret. It starts with `whsec_`.
8. Paste that secret into Render as `STRIPE_WEBHOOK_SECRET`. Save. Render redeploys.
9. Check `/health` again. `stripe_configured` should be true.

## 5. Make a test payment

1. Open the installment Payment Link for `MMI2610ROT | Speed Wealth Book`.
2. Pay with the test card `4242 4242 4242 4242`, any future expiry, any CVC.
3. Open the SR Event Sales sheet, tab `MMI2610ROT`.
4. The row should appear in the light green Speed Wealth Book group: programme `Speed Wealth Book`, payment plan `full`, status `succeeded`, and a Payment ID. Speed Wealth Book is always `full`. For Never Work Again, GBI, TTT, EWC, and Quantum Leap, payment plan is `full` when the amount equals the suggested amount on that Stripe price. If the customer pays less, payment plan is `installment` and Installment (remaining) is the suggested amount minus what they paid.
5. A later payment for the same programme is inserted into that same group. The other programme groups stay where they are, with three empty rows between groups.

A refund moves that row into the light red Refunded group, after QL, only after `charge.refunded` is added to the destination. Until then, a refund in Stripe does not change the sheet. A one-time full payment is written only after `checkout.session.completed` is added.

A product whose name does not contain the event code, a space, a vertical bar, a space, and the programme is ignored. Stripe still gets a success response, and nothing is written to the sheet.
