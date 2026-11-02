"""Email support tickets + live chats across every status the dashboard
filters on (Open/Replied/Resolved for email; Waiting/Active/Closed for
chat) — messages go through SupportMessage's normal text property, so
they're encrypted at rest exactly like real ones."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.support_ticket import SupportMessage, SupportTicket
from app.models.user import User
from seeders import config
from seeders.config import rng

_CATEGORIES = [
    "Billing & Payouts", "Technical issue", "Voting & Elections",
    "Events & Ticketing", "Account & Verification", "Other",
]
_EMAIL_STATUSES = ["Open", "Replied", "Resolved"]

# (subject, category, conversation)  each conversation is a list of (sender_type, text) tuples
_TICKET_SCENARIOS = [
    (
        "Payout hasn't arrived after 5 business days",
        "Billing & Payouts",
        [
            ("customer", "Hi, I requested a payout on Monday and it's now Friday — still nothing in my account. Can you check what's happening?"),
            ("agent", "Thanks for reaching out! I've pulled up your payout request. It looks like it's still in a pending state with the processor. I'll escalate this to the finance team and get back to you within 24 hours."),
            ("customer", "Appreciate the quick response. It's quite urgent as I need those funds before end of month."),
            ("agent", "Understood — I've marked this as urgent. You should see the funds within 1–2 business days. I'll send you a confirmation email once it clears."),
        ],
    ),
    (
        "KYC documents keep getting rejected",
        "Account & Verification",
        [
            ("customer", "I've uploaded my ID three times now and each time it says 'document unclear'. The photo looks perfectly fine to me. What am I doing wrong?"),
            ("agent", "I can see the three attempts in our system. The issue is that our verification provider is detecting a glare in the top-right corner of your ID. Try taking the photo in natural light without flash and ensure the ID is flat on a dark surface."),
            ("customer", "That worked! Just submitted it again — fingers crossed."),
        ],
    ),
    (
        "Voter receipt code not recognised at polling station",
        "Voting & Elections",
        [
            ("customer", "Several of our voters are reporting that their receipt codes aren't being accepted by the scanner. The election is happening right now — this is an emergency."),
            ("agent", "I'm treating this as high priority. Can you tell me the election ID and roughly how many voters are affected?"),
            ("customer", "Election ID is EL-2024-0891, about 12 voters so far out of 200."),
            ("agent", "I can see the election is live. The issue is a sync delay with the receipt validator — I've forced a refresh on our end. Please ask those voters to try again now. New codes generated after 09:00 today should all work."),
            ("customer", "That fixed it! All 12 went through just now. Thank you so much."),
            ("agent", "Great to hear! I'll keep monitoring the election for you. Let me know if anything else comes up."),
        ],
    ),
    (
        "Can't add a second admin to my organization",
        "Account & Verification",
        [
            ("customer", "When I go to Settings > Team and try to invite a new admin, the button is greyed out. Is this a plan restriction?"),
            ("agent", "Good question — the Free plan supports one admin only. Upgrading to Starter or above unlocks up to 5 admin seats. Would you like me to send over a comparison of the plans?"),
            ("customer", "Yes please, that would be helpful."),
        ],
    ),
    (
        "QR code on event ticket shows invalid",
        "Events & Ticketing",
        [
            ("customer", "Attendees at our event tonight are getting 'invalid ticket' when the scanner reads their QR codes. We've already let in 40 people with no issue — it seems to be affecting a specific batch."),
            ("agent", "I'm looking at your event now. It appears a batch of 23 tickets generated between 14:00 and 15:30 yesterday have a corrupted QR payload. I can regenerate and re-send those tickets immediately if you give me the go-ahead."),
            ("customer", "Yes please do that right away."),
            ("agent", "Done — all 23 attendees have been emailed new tickets. The regenerated codes are already working in the scanner."),
            ("customer", "Perfect, crisis averted. Thank you!"),
        ],
    ),
    (
        "Request refund for accidental duplicate purchase",
        "Billing & Payouts",
        [
            ("customer", "I accidentally bought two tickets to the same event. Can I get a refund for one of them?"),
            ("agent", "Of course! I can see the duplicate purchase in your account. I've initiated a refund for one ticket — it should appear on your card within 3–5 business days."),
            ("customer", "That was fast, thank you!"),
        ],
    ),
    (
        "Election results not showing after polls closed",
        "Voting & Elections",
        [
            ("customer", "Our election closed 2 hours ago but the results page still shows 'Results pending'. When will they be available?"),
            ("agent", "The results are available now — it looks like you might not have published them yet. In your election dashboard, go to Results > Publish Results to make them visible to your voters."),
            ("customer", "Oh! I didn't realise I had to manually publish. Done — thanks for the fast reply."),
        ],
    ),
    (
        "Nominee profile photos not uploading",
        "Technical issue",
        [
            ("customer", "When I try to upload a profile photo for any nominee it just spins forever and then fails with a generic error."),
            ("agent", "Thanks for flagging this. Are you seeing this across all nominees or just specific ones? Also, what file format and size are the photos?"),
            ("customer", "All nominees. The photos are HEIC format from an iPhone, around 4–6 MB each."),
            ("agent", "HEIC isn't supported yet — please convert them to JPG or PNG first. Any standard converter app will work. We're tracking HEIC support as a feature request for a future release."),
            ("customer", "Converted to JPG and they're uploading fine now. Might be worth mentioning that somewhere in the UI."),
            ("agent", "Agreed — I've filed a UX improvement ticket to add accepted format info to the upload dialog. Thanks for the feedback!"),
        ],
    ),
    (
        "Promo code not applying at checkout",
        "Billing & Payouts",
        [
            ("customer", "I was given the promo code LAUNCH20 but when I enter it at checkout it says 'code not found'."),
            ("agent", "I can see LAUNCH20 in our system — it's valid. Can you tell me which election or event you're trying to apply it to?"),
            ("customer", "It's for the Community Board Election 2025."),
            ("agent", "Ah — that code is scoped to events only, not elections. I'll note that the code description wasn't clear about this. Let me create a new code that covers elections and send it to you directly."),
        ],
    ),
    (
        "Nomination form submissions missing after export",
        "Voting & Elections",
        [
            ("customer", "I exported the nomination form responses to CSV but only 4 of the 17 submissions came through. Where are the others?"),
            ("agent", "I can see 17 submissions in the database. The missing ones were submitted before you added the new fields to the form — those records have null values in the new columns, which caused the export query to exclude them. I've fixed the export and attached a corrected CSV."),
            ("customer", "Got it, thank you. Is there a way to view them in the dashboard directly?"),
            ("agent", "Yes — under Nominations > Submissions, all 17 are visible regardless of completeness. The CSV export fix will go out in this week's release."),
        ],
    ),
    (
        "Two-factor authentication code not accepted",
        "Technical issue",
        [
            ("customer", "I set up 2FA yesterday and today my authenticator code keeps saying invalid. I'm definitely using the right account."),
            ("agent", "The most common cause is a clock drift on your phone — your authenticator app needs to be within 30 seconds of the correct time. On iOS: Settings > General > Date & Time > Set Automatically. On Android: Settings > System > Date & Time > Use network-provided time."),
            ("customer", "That fixed it — my phone's clock was 2 minutes off. Thanks!"),
        ],
    ),
    (
        "Live chat queue not visible to agents",
        "Technical issue",
        [
            ("customer", "My support agents say they can't see the live chat queue even though they have the 'Support Agent' role assigned."),
            ("agent", "Thanks for the report. I've checked the role permissions — Support Agent needs the 'Live Chat Access' permission which wasn't included in the default role template. I've updated the role on your account. Your agents will need to log out and back in."),
            ("customer", "They logged out and the queue is visible now. We had no idea that permission existed."),
            ("agent", "We're improving the role setup wizard to make this clearer. Sorry for the confusion!"),
        ],
    ),
    (
        "Voter list CSV import fails with no error message",
        "Voting & Elections",
        [
            ("customer", "I'm trying to import a voter list via CSV but it just silently fails — no success message, no error, nothing happens."),
            ("agent", "Could you share the first few rows of your CSV? We need to check the column headers — they're case-sensitive."),
            ("customer", "Sure — my headers are: Name, Email, Phone, VoterID"),
            ("agent", "Found it — the expected header for voter ID is 'voter_id' (lowercase with underscore). Rename that column and the import should work."),
            ("customer", "That was it! All 312 voters imported successfully."),
        ],
    ),
    (
        "Payout blocked — identity verification required",
        "Billing & Payouts",
        [
            ("customer", "I'm trying to request a payout but the button says 'Verify your identity first'. I already verified when I signed up."),
            ("agent", "Payout verification is a separate step from account verification — it requires a government-issued ID to comply with financial regulations. You can start the process under Settings > Payouts > Verify Identity. It usually takes under 10 minutes."),
            ("customer", "Okay, starting it now. Do I need to do this every time?"),
            ("agent", "No — once verified, it's permanent unless your ID expires or you change your legal name. You'll only need to redo it in those cases."),
        ],
    ),
    (
        "Event ticketing page shows wrong currency",
        "Events & Ticketing",
        [
            ("customer", "Our event page is showing prices in USD but all our attendees are in Ghana. Can we change it to GHS?"),
            ("agent", "Absolutely — go to your event settings and under Pricing, select GHS from the currency dropdown. Note that existing tickets already sold will keep their original currency; only new purchases will use GHS."),
            ("customer", "Changed it, looks correct now. Thanks!"),
        ],
    ),
    (
        "Election closed early — can votes be accepted retroactively?",
        "Voting & Elections",
        [
            ("customer", "A network issue on our end meant the election portal was inaccessible for 2 hours before it was supposed to close. We have voters who missed the window. Can we extend the deadline retroactively?"),
            ("agent", "I understand the situation. Unfortunately we can't add votes retroactively as it would compromise the audit trail. What I can do is help you re-open the election for a fixed 2-hour window if all organisation admins consent. This will be logged as an official extension. Would you like to proceed?"),
            ("customer", "Yes, please go ahead. All admins are in agreement."),
            ("agent", "Extension applied — the election is now open until 17:00 today. An audit note has been added explaining the extension and the reason. Voters who haven't yet cast their ballot will receive an email."),
        ],
    ),
    (
        "SMS notifications not being delivered",
        "Technical issue",
        [
            ("customer", "We enabled SMS notifications for our election but none of the voters seem to be receiving them."),
            ("agent", "I can see 0 SMS deliveries for your election. Your organisation is on the Free plan — SMS notifications are a paid feature. You'd need to upgrade or use email notifications instead."),
            ("customer", "I see. We'll stick with email for now. Thanks for the quick answer."),
        ],
    ),
    (
        "Organisation name change not reflected on public pages",
        "Account & Verification",
        [
            ("customer", "We updated our organisation name three days ago but our public election pages still show the old name."),
            ("agent", "The name update was saved successfully in your profile, but public pages are cached for up to 72 hours. The change should propagate by tomorrow at the latest. If it still shows the old name after that, please let me know."),
            ("customer", "It updated overnight. All good now!"),
        ],
    ),
    (
        "Bulk email invitations bouncing",
        "Technical issue",
        [
            ("customer", "I sent voting invitations to 500 emails and our mail delivery report shows 47 bounces. How do I find out which ones failed?"),
            ("agent", "Under Elections > Voters > Invitation Status you can filter by 'Bounced'. The bounce report is also exportable as CSV from that view."),
            ("customer", "Found it, very helpful. Most seem to be typos in the email addresses. Is there a way to correct and resend?"),
            ("agent", "Yes — click any bounced voter row, edit the email address, and use the 'Resend Invitation' button. Each one has to be corrected individually for now; bulk-edit is on our roadmap."),
        ],
    ),
    (
        "Can't download my organisation's activity report",
        "Technical issue",
        [
            ("customer", "Every time I try to download the monthly activity report it gives a 500 error after a few seconds."),
            ("agent", "Thanks for the report. I'm seeing a timeout in our logs — your organisation has a large amount of data and the report generator is hitting its limit. I've manually generated the report on our end and attached it here. We'll fix the timeout in the next patch."),
            ("customer", "Got the attachment, exactly what I needed. Thanks!"),
        ],
    ),
]


async def seed_support_tickets(db: AsyncSession, voters: list[User]) -> None:
    email_count = (
        await db.execute(select(func.count()).select_from(SupportTicket).where(SupportTicket.channel == "email"))
    ).scalar_one()

    now = datetime.now(timezone.utc)
    scenarios = list(_TICKET_SCENARIOS)
    rng.shuffle(scenarios)

    for i in range(email_count, config.N_SUPPORT_EMAIL_TICKETS):
        voter = rng.choice(voters)
        scenario = scenarios[i % len(scenarios)]
        subject, category, conversation = scenario

        # Weight toward Open/Replied so inbox has interesting unread state
        status_weights = ["Open", "Open", "Replied", "Replied", "Resolved"]
        status = rng.choice(status_weights)

        # Trim conversation to match status: Open → 1 msg, Replied → 2+, Resolved → full
        if status == "Open":
            msgs = conversation[:1]
        elif status == "Replied":
            msg_count = rng.randint(2, max(2, len(conversation) - 1))
            msgs = conversation[:msg_count]
            # ensure last message is agent for "Replied"
            if msgs[-1][0] == "customer" and len(msgs) < len(conversation):
                msgs = conversation[:msg_count + 1]
        else:
            msgs = conversation

        base_time = now - timedelta(days=rng.randint(1, 45))
        ticket = SupportTicket(
            user_id=voter.user_id,
            subject=subject,
            category=category,
            channel="email",
            status=status,
            priority=rng.random() < 0.2,
            created_at=base_time,
            updated_at=base_time + timedelta(hours=len(msgs)),
        )
        db.add(ticket)
        await db.flush()

        for j, (sender_type, text) in enumerate(msgs):
            db.add(SupportMessage(
                ticket_id=ticket.ticket_id,
                sender_type=sender_type,
                sender_user_id=voter.user_id,
                text=text,
                created_at=base_time + timedelta(hours=j * rng.randint(1, 8)),
            ))

    await db.flush()


async def seed_live_chats(db: AsyncSession, voters: list[User], agents: list[User]) -> None:
    chat_count = (
        await db.execute(select(func.count()).select_from(SupportTicket).where(SupportTicket.channel == "chat"))
    ).scalar_one()

    now = datetime.now(timezone.utc)
    seen_names: set = set()
    for i in range(chat_count, config.N_SUPPORT_CHATS):
        voter = rng.choice(voters)
        state = rng.choice(["waiting", "waiting", "active", "closed", "closed", "closed"])
        status = {"waiting": "Waiting", "active": "Active", "closed": "Closed"}[state]
        agent = rng.choice(agents) if state != "waiting" else None

        chat = SupportTicket(
            user_id=voter.user_id,
            subject="Live chat",
            category="Live Chat",
            channel="chat",
            status=status,
            assigned_agent_id=agent.user_id if agent else None,
        )
        db.add(chat)
        await db.flush()
        db.add(SupportMessage(
            ticket_id=chat.ticket_id, sender_type="customer", sender_user_id=voter.user_id,
            text="Hi, I need help with something urgent.", created_at=now - timedelta(hours=rng.randint(1, 200)),
        ))
        if agent:
            db.add(SupportMessage(
                ticket_id=chat.ticket_id, sender_type="agent", sender_user_id=agent.user_id,
                text="Hi! I'm here to help — can you tell me more?", created_at=now - timedelta(hours=rng.randint(0, 100)),
            ))

    await db.flush()
