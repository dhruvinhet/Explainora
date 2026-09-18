import { useEffect, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import styles from './Legal.module.css'

export const SUPPORT_EMAIL = 'support@explainora.dev'
const LAST_UPDATED = '30 August 2026'

type LegalPageProps = {
  eyebrow: string
  title: string
  summary: string
  children: ReactNode
}

function LegalPage({ eyebrow, title, summary, children }: LegalPageProps) {
  useEffect(() => {
    const previous = document.title
    document.title = `${title} · Explainora`
    return () => { document.title = previous }
  }, [title])

  return <main className={styles.page}>
    <div className={styles.grid} aria-hidden="true" />
    <header className={styles.header}>
      <Link className={styles.brand} to="/" aria-label="Explainora home"><span>E</span><strong>EXPLAINORA</strong></Link>
      <nav aria-label="Policy navigation"><Link to="/privacy">Privacy</Link><Link to="/terms">Terms</Link><Link to="/refunds">Refunds</Link><Link to="/support">Support</Link></nav>
    </header>
    <div className={`shell ${styles.layout}`}>
      <aside className={styles.aside}>
        <span>{eyebrow}</span>
        <p>Last updated<br /><b>{LAST_UPDATED}</b></p>
        <Link to="/">← Back to home</Link>
      </aside>
      <article className={styles.document}>
        <div className={styles.title}><h1>{title}</h1><p>{summary}</p></div>
        {children}
      </article>
    </div>
    <footer className={styles.footer}><div className="shell"><span>© 2026 Explainora</span><div><Link to="/privacy">Privacy</Link><Link to="/terms">Terms</Link><Link to="/refunds">Refunds</Link><Link to="/support">Support</Link></div></div></footer>
  </main>
}

function ContactLine({ subject }: { subject: string }) {
  return <p>Email <a href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(subject)}`}>{SUPPORT_EMAIL}</a>. Include the email associated with your account and enough detail for us to identify the request. Never send your password, recovery link, API key, or full payment-card details.</p>
}

export function PrivacyPolicy() {
  return <LegalPage eyebrow="YOUR DATA" title="Privacy Policy" summary="This policy explains what Explainora collects, why we use it, where it goes, and the choices available to you.">
    <section><h2>1. Who this policy covers</h2><p>Explainora is the AI-assisted video explanation service available at explainora.dev. In this policy, “Explainora”, “we”, “us”, and “our” refer to the operator of that service. This policy applies to visitors, account holders, and people who contact support.</p></section>
    <section><h2>2. Information we collect</h2><ul>
      <li><b>Account information:</b> name, email address, account identifier, profile settings, plan, quota usage, and account dates.</li>
      <li><b>Content you provide:</b> prompts, questions, uploaded documents, extracted text, style and audience selections, and support messages.</li>
      <li><b>Generated content:</b> scripts, scenes, narration, images, videos, manifests, quality evaluations, and related asset metadata.</li>
      <li><b>Usage and technical information:</b> job status, timestamps, generation events, provider usage and estimated cost, request identifiers, operational logs, browser session data, and security events. Security audits use a keyed pseudonymous identifier rather than storing your raw email or user ID.</li>
      <li><b>Billing information:</b> plan, transaction status, provider references, invoice-related details, and refund status after paid billing launches. Payment providers collect card, UPI, bank, or wallet credentials directly; Explainora does not receive or store complete payment credentials.</li>
    </ul></section>
    <section><h2>3. How we use information</h2><p>We process information to create and deliver requested videos; authenticate and secure accounts; store your private library; enforce plan limits; recover interrupted jobs; provide support; prevent abuse; measure reliability and cost; process verified payments and refunds; comply with law; and improve service reliability and quality. We do not sell personal information, use private prompts and uploads to advertise to you, or use them to train our own general-purpose AI model.</p></section>
    <section><h2>4. AI processing and service providers</h2><p>Generating a video requires sending relevant prompt or document text, instructions, and scene context to the configured language-model provider. Depending on deployment configuration, this may include Groq, Cerebras, NVIDIA, or OpenRouter and models made available through them. Do not submit confidential, regulated, or third-party personal data unless you are authorised to do so.</p><p>We also use service providers for authentication, database and private object storage (Supabase), hosting and rendering (Oracle Cloud), network security and delivery (Cloudflare, where enabled), transactional email, monitoring, and payments (such as Razorpay when billing is enabled). They process information for us under their own contractual and legal obligations.</p></section>
    <section><h2>5. Storage, security, and international processing</h2><p>Account records and private media are protected through authentication, row-level database controls, private Storage buckets, short-lived media links, encryption in transit, and restricted server credentials. No online service can promise absolute security. Providers may process or store information outside your state or country, subject to applicable transfer restrictions.</p></section>
    <section><h2>6. Retention</h2><p>We keep account and generated-content records while your account is active or as needed to provide the service. Temporary rendering files and logs are kept only while reasonably needed for rendering, recovery, security, and incident investigation, then deleted or anonymised under operational retention procedures. Pseudonymous account-security audit records are retained for up to 365 days. Payment, tax, fraud-prevention, dispute, and backup records may be retained longer where required by law or legitimate operational obligations.</p></section>
    <section><h2>7. Your choices and rights</h2><p>You may access core profile and plan information in My Account, change your password, request password recovery, or permanently delete your account. Confirmed deletion removes the Auth identity, associated application records, and private Storage objects, except information we must retain under law, for payment disputes, or in time-limited backups.</p><p>You may also ask for access, correction, erasure, information about processing, withdrawal of consent where consent applies, or grievance handling. We may need to verify your identity before acting. Withdrawing consent does not affect processing already lawfully completed and may prevent us from providing features that need the information.</p></section>
    <section><h2>8. Children</h2><p>The service is not directed to people under 18 and they must not create an account or submit personal data without a legally valid parent or guardian process. Contact us if you believe a child has provided data so we can investigate and remove it where appropriate.</p></section>
    <section><h2>9. Cookies and local storage</h2><p>We use essential browser storage for authentication sessions, recovery state, account display, and necessary preferences. We do not currently operate an advertising-cookie programme. If non-essential analytics or advertising technologies are introduced, this policy and the applicable consent controls will be updated first.</p></section>
    <section><h2>10. Changes and contact</h2><p>We may update this policy as the product, providers, or law changes. Material changes will be communicated through the service or account email where appropriate.</p><ContactLine subject="Privacy request" /></section>
  </LegalPage>
}

export function TermsOfService() {
  return <LegalPage eyebrow="THE AGREEMENT" title="Terms of Service" summary="These terms govern access to Explainora. Please read them before creating an account or generating a video.">
    <section><h2>1. Acceptance and eligibility</h2><p>By accessing Explainora, creating an account, or using the service, you agree to these Terms and the Privacy Policy. You must be at least 18, able to enter a binding agreement, and authorised to act for any organisation whose content you submit. If you disagree, do not use the service.</p></section>
    <section><h2>2. The service</h2><p>Explainora converts prompts and source material into AI-assisted explanatory videos. Features, model providers, quotas, duration limits, formats, and availability may change as the service develops. Beta or preview features may be incomplete, changed, or withdrawn.</p></section>
    <section><h2>3. Accounts</h2><p>Provide accurate information, protect your password and recovery links, and notify us promptly of suspected unauthorised access. You are responsible for activity under your account except where applicable law provides otherwise. We may require reauthentication for sensitive actions and may limit concurrent jobs or abusive traffic.</p></section>
    <section><h2>4. Your content</h2><p>You retain ownership of content you submit and, as between you and Explainora, your generated outputs to the extent permitted by law. You grant us a limited, worldwide licence to host, copy, transform, transmit, and process that content only to operate, secure, support, and improve the service. This licence ends when the content is deleted, except for lawful retention, dispute records, and time-limited backups.</p><p>You represent that you have all rights and permissions needed for submitted content. Do not upload confidential information, health or financial records, government identifiers, or another person’s personal data unless you have a lawful basis and the service is appropriate for that use.</p></section>
    <section><h2>5. Acceptable use</h2><p>Do not use Explainora to violate law or third-party rights; create deceptive impersonation, fraud, harassment, sexual exploitation, or hateful or violent abuse; distribute malware; evade quotas or security controls; probe or disrupt infrastructure; reverse engineer protected components except where law permits; or generate content that presents harmful professional advice as verified fact. We may investigate, limit, remove, or report unlawful use.</p></section>
    <section><h2>6. AI output and your responsibility</h2><p>AI-generated material can be inaccurate, incomplete, biased, or similar to material generated for others. Review every output before publishing or relying on it. Explainora is not a substitute for medical, legal, financial, safety, academic, or other professional judgment, and outputs are not guaranteed to be unique, error-free, or fit for a particular purpose.</p></section>
    <section><h2>7. Plans, payment, and cancellation</h2><p>Free and paid plans may include different quotas, maximum durations, priorities, and features. Prices and taxes shown at checkout control for that transaction. A paid plan becomes active only after verified provider confirmation. Recurring plans may be cancelled before the next renewal and remain active through the paid period unless stated otherwise. Refund eligibility is governed by the Refund and Cancellation Policy and mandatory consumer rights.</p></section>
    <section><h2>8. Intellectual property</h2><p>Explainora, its interface, code, design system, branding, and service materials are owned by us or our licensors. These Terms give you a limited, revocable, non-transferable right to use the service; they do not transfer ownership of the service or third-party models, fonts, icons, music, or other licensed materials.</p></section>
    <section><h2>9. Suspension and termination</h2><p>You may stop using the service or delete your account. We may suspend or terminate access for material breach, fraud, security risk, non-payment, unlawful use, or where required by law. Where practicable, we will provide notice and an opportunity to address the issue. Account deletion is permanent and cannot be undone.</p></section>
    <section><h2>10. Availability and warranties</h2><p>We work to provide a reliable service, but generation may fail or be delayed by model providers, rendering capacity, networks, maintenance, or events outside reasonable control. To the extent permitted by law, the service is provided “as is” and “as available”. Nothing in these Terms excludes warranties, remedies, or rights that cannot lawfully be excluded.</p></section>
    <section><h2>11. Liability</h2><p>To the extent permitted by law, neither party is liable for indirect or consequential loss that was not reasonably foreseeable. Our aggregate liability relating to paid service will not exceed the amount you paid us during the six months preceding the event giving rise to the claim. These limits do not apply to fraud, wilful misconduct, breach of confidentiality, infringement, personal injury caused by negligence, or liability that applicable law does not allow us to limit.</p></section>
    <section><h2>12. Governing law and disputes</h2><p>These Terms are governed by the laws of India. Please contact support first so we can attempt a fair resolution. Nothing here prevents a consumer from using a competent Consumer Commission, the National Consumer Helpline, or another forum available under applicable law.</p></section>
    <section><h2>13. Changes and contact</h2><p>We may update these Terms prospectively. Material changes will be communicated before they take effect where reasonably practicable. Continued use after the effective date constitutes acceptance, except where law requires fresh consent.</p><ContactLine subject="Terms question" /></section>
  </LegalPage>
}

export function RefundPolicy() {
  return <LegalPage eyebrow="FAIR BILLING" title="Refund & Cancellation Policy" summary="Paid billing is not live yet. This policy will govern purchases once verified payment processing is enabled.">
    <section><h2>1. Cancel anytime</h2><p>You may cancel a recurring plan through the billing portal once subscriptions launch, or by contacting support. Cancellation stops future renewals. Unless a refund is approved or law requires otherwise, access continues until the end of the period already paid for.</p></section>
    <section><h2>2. Refund eligibility</h2><p>We will provide a full refund for a duplicate charge, a successful payment that does not activate the purchased plan within a reasonable time, or a charge we cannot lawfully retain. You may also request a full refund within seven calendar days of the initial purchase if you have not generated a video or materially consumed the plan’s included allowance.</p><p>After included benefits have been materially used, fees are generally non-refundable, but we will consider a proportional or full remedy where the paid service was materially unavailable, the wrong plan was provisioned, or applicable consumer law requires it.</p></section>
    <section><h2>3. Renewals, upgrades, and downgrades</h2><p>Renewal charges follow the billing interval displayed at checkout. Upgrade or downgrade timing, proration, and effective dates will be shown before confirmation. Cancellation does not automatically erase your account or generated videos.</p></section>
    <section><h2>4. How to request a refund</h2><p>Email support within 30 days of the charge with your account email, payment reference, charge date, requested remedy, and a short explanation. Do not send complete card, bank, UPI, or wallet credentials. We aim to acknowledge requests within two business days and communicate a decision within ten business days.</p></section>
    <section><h2>5. Approved refunds</h2><p>Approved refunds are returned to the original payment method wherever possible. Provider and bank processing times vary and may take 5–10 business days after approval. Taxes and currency conversion are handled according to the original transaction and applicable law.</p></section>
    <section><h2>6. Chargebacks and statutory rights</h2><p>Please contact us first so we can investigate quickly; this does not restrict your right to contact your payment provider or a consumer authority. Nothing in this policy limits a refund, reversal, cancellation, or other remedy that applicable law guarantees.</p><ContactLine subject="Refund request" /></section>
  </LegalPage>
}

export function Support() {
  return <LegalPage eyebrow="HUMAN HELP" title="Support & Contact" summary="Account, generation, privacy, and future billing support through one monitored address.">
    <section className={styles.contactCard}><span>PRIMARY CONTACT</span><h2><a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a></h2><p>We aim to acknowledge ordinary requests within two business days. Account-security reports receive priority. Support is currently provided in English.</p><a className={styles.emailButton} href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent('Explainora support request')}`}>EMAIL SUPPORT <b>→</b></a></section>
    <section><h2>What to include</h2><ul><li>The email associated with your account.</li><li>The video or job ID, if the issue concerns generation.</li><li>A concise description and the approximate time the problem occurred.</li><li>A screenshot with secrets and personal information removed, if useful.</li></ul><p>Never send a password, recovery URL, API key, authentication token, complete payment-card number, CVV, OTP, or bank password.</p></section>
    <section><h2>Privacy and account requests</h2><p>Use the subject “Privacy request” for access, correction, deletion, consent, or data-processing questions. Account deletion is also available directly in My Account and requires your current password plus explicit confirmation.</p></section>
    <section><h2>Refund and billing requests</h2><p>Use the subject “Refund request” and include the payment reference—not full payment credentials. The Refund and Cancellation Policy explains eligibility and expected processing times.</p></section>
    <section><h2>Consumer escalation</h2><p>We want the opportunity to resolve complaints directly. If an Indian consumer remains dissatisfied, the Government of India’s <a href="https://consumerhelpline.gov.in/" rel="noreferrer" target="_blank">National Consumer Helpline</a> offers a pre-litigation grievance channel, including portal access and helpline 1915. This information does not limit any legal remedy.</p></section>
  </LegalPage>
}
