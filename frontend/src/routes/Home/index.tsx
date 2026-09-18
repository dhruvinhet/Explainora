import { Composer } from './components/Composer'
import { ExamplesBento } from './components/ExamplesBento'
import { Footer } from './components/Footer'
import { Hero } from './components/Hero'
import { ProcessSteps } from './components/ProcessSteps'

export function Home() {
  return (
    <section aria-label="Home">
      <Hero />
      <Composer />
      <ExamplesBento />
      <ProcessSteps />
      <Footer />
    </section>
  )
}
