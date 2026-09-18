import { ButtonLink } from '../../components/Button/Button'
import { LandingExamples } from './Examples/Examples'
import { Features } from './Features/Features'
import { LandingFooter } from './Footer/Footer'
import { LandingHero } from './Hero/Hero'
import { HowItWorks } from './HowItWorks/HowItWorks'
import { LandingNav } from './Nav/Nav'
import { Showcase } from './Showcase/Showcase'
import { TrustStrip } from './TrustStrip/TrustStrip'
import navStyles from './Nav/Nav.module.css'

export function Landing() {
  return (
    <>
      <LandingNav />
      <main>
        <LandingHero />
        <TrustStrip />
        <HowItWorks />
        <LandingExamples />
        <Showcase />
        <Features />
        <LandingFooter />
      </main>
      <ButtonLink arrow aria-label="Start explaining in Explainora" className={navStyles.mobileDock} to="/app" viewTransition>Start explaining</ButtonLink>
    </>
  )
}
