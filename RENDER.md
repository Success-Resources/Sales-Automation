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

Add a price, for example 10.00 EUR, one-time. Create a Payment Link for that price. The QR code later is that link. For the test, open the link in a browser.

The other Rotterdam products, when you are ready, use the same left side and a different programme:

- `MMI2610ROT | Quantum Leap 2027`
- `MMI2610ROT | Enlightened Warrior Camp 2027`
- `MMI2610ROT | Train the Trainer 2027`
- `MMI2610ROT | Guerrilla Business Intensive 2027`
- `MMI2610ROT | Never Work Again 2027`
- `MMI2610ROT | Speed Wealth Book`

A full payment and an installment plan are two prices on that product. Each price has its own Payment Link.

## 4. Stripe webhook

1. Stripe → Developers → Webhooks → Add endpoint.
2. Endpoint URL: `https://<your-service>.onrender.com/stripe/webhook`
3. Select these events: `checkout.session.completed`, `invoice.paid`, `charge.refunded`.
4. Add endpoint. Open it and reveal the signing secret.
5. Paste that secret into Render as `STRIPE_WEBHOOK_SECRET`. Save. Render redeploys.
6. Check `/health` again. `stripe_configured` should be true.

## 5. Make a test payment

1. Open the Payment Link for `MMI2610ROT | Speed Wealth Book`.
2. Pay with the test card `4242 4242 4242 4242`, any future expiry, any CVC.
3. Open the SR Event Sales sheet, tab `MMI2610ROT`.
4. One row should appear: programme `Speed Wealth Book`, payment plan `full`, status `succeeded`, and a Payment Intent id.
5. In Stripe, refund that test payment. The same row should change to `refunded`. A second row should not appear.

An installment price records one row per paid invoice, with the same Subscription id and installment numbers 1, 2, 3.

A product whose name does not contain the event code, a space, a vertical bar, a space, and the programme is ignored. Stripe still gets a success response, and nothing is written to the sheet.
