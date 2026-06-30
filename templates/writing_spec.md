# Meeting recap — writing spec

This file is the **writing guidance** fed to the generation step for INTERNAL
recaps and EXTERNAL/SALES internal debriefs. It is intentionally generic — edit
it to match your team's voice, sections, and priorities. (The client-facing
template lives separately in `run_recap.py` as `CLIENT_SPEC` so internal framing
can never leak into a client email.)

General standards for every recap:

1. Specificity over generality. Capture decisions, named owners, dates, numbers,
   tools, and exact commitments — not vague summaries.
2. Lead with the decision. Each topic section should state what was decided, not
   just what was discussed.
3. Pull action items from the transcript, not only the auto-summary. Informal
   commitments ("I'll set that up") count.
4. Group intelligently. A 30-min call has ~3-5 sections; a long call ~6-8.
5. Always link the recording/transcript at the bottom of INTERNAL recaps.

Priority tags: 🔴 Today · 🟡 This week · 🟢 Ongoing.

---

## Email HTML Structure — Internal Meetings

Use for standups, planning, retros, and other all-internal meetings.

```html
<html><body style="font-family: Arial, sans-serif; color: #333; line-height: 1.7; max-width: 800px;">

<p>Team,</p>
<p>[1-2 sentence overview of what the meeting covered and how long it ran.]</p>

<hr style="border: none; border-top: 2px solid #1a73e8; margin: 24px 0;">

<h2 style="color: #1a73e8;">🧭 Meeting Overview</h2>
<p>[3-5 sentence high-level summary of the major topics.]</p>

<hr style="border: none; border-top: 1px solid #ddd; margin: 24px 0;">

<!-- One numbered section per major topic -->
<h2 style="color: #1a73e8;">1. [Topic Title]</h2>
<p><strong>Decision:</strong> [State it clearly if one was made.]</p>
<ul>
  <li><strong>[Sub-topic]:</strong> [Specifics — names, tools, numbers.]</li>
</ul>

<hr style="border: none; border-top: 2px solid #1a73e8; margin: 24px 0;">

<h2 style="color: #1a73e8;">✅ Action Items by Owner</h2>
<h3 style="color: #333; border-bottom: 1px solid #ddd; padding-bottom: 4px;">[Person]</h3>
<table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
  <tr style="background: #f8f9fa;">
    <td style="padding: 8px; border: 1px solid #ddd;">[Specific, actionable item]</td>
    <td style="padding: 8px; border: 1px solid #ddd; width: 110px; text-align: center;">🔴 Today</td>
  </tr>
</table>

<hr style="border: none; border-top: 2px solid #1a73e8; margin: 24px 0;">
<p>Full transcript and recording available on <a href="[transcript URL]">Fireflies</a>.</p>
<p>– [Sender name]</p>
</body></html>
```

---

## Email HTML Structure — External / Sales Calls (internal debrief)

Use when an outside guest was present. **This email is INTERNAL ONLY — never
sent to the guest.** Keep it factual and useful to your team. Adapt the sections
to whatever your team actually tracks.

```html
<html><body style="font-family: Arial, sans-serif; color: #333; line-height: 1.7; max-width: 800px;">

<p>Team,</p>
<p>[1-2 sentence context: who we met with, what kind of call, the headline takeaway.]</p>

<hr style="border: none; border-top: 2px solid #1a73e8; margin: 24px 0;">

<h2 style="color: #1a73e8;">🏢 Who We Met</h2>
<ul>
  <li><strong>Company / people:</strong> [Names + roles from their side.]</li>
  <li><strong>Our side:</strong> [Who attended from our team.]</li>
</ul>

<h2 style="color: #1a73e8;">🎯 Their Needs</h2>
<ul>
  <li>[What they're trying to solve, in their own words where useful.]</li>
</ul>

<h2 style="color: #1a73e8;">💡 What We Discussed / Showed</h2>
<ul>
  <li>[What we proposed or demoed and how they responded.]</li>
</ul>

<h2 style="color: #1a73e8;">⚠️ Open Questions & Concerns</h2>
<ul>
  <li>[Anything unresolved or that needs follow-up.]</li>
</ul>

<hr style="border: none; border-top: 2px solid #1a73e8; margin: 24px 0;">

<h2 style="color: #1a73e8;">✅ Follow-Up Actions</h2>
<table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
  <tr style="background: #f8f9fa;">
    <td style="padding: 8px; border: 1px solid #ddd;">[What we committed to / internal prep]</td>
    <td style="padding: 8px; border: 1px solid #ddd; width: 90px; text-align: center;">[Owner]</td>
    <td style="padding: 8px; border: 1px solid #ddd; width: 110px; text-align: center;">🔴 Today</td>
  </tr>
</table>

<hr style="border: none; border-top: 2px solid #1a73e8; margin: 24px 0;">
<p>Full transcript and recording available on <a href="[transcript URL]">Fireflies</a>.</p>
<p>– [Sender name]</p>
</body></html>
```
