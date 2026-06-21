import Link from '@docusaurus/Link'
import Layout from '@theme/Layout'

export default function Home(): JSX.Element {
  return (
    <Layout
      title="Foresight Memory Architecture"
      description="Composable memory for AI agents"
    >
      <main
        style={{ margin: '0 auto', maxWidth: '960px', padding: '4rem 1.5rem' }}
      >
        <h1>Foresight Memory Architecture</h1>
        <p>
          Domain-agnostic, composable memory for AI agents with persistent
          context, event sourcing, and structured memory blocks.
        </p>
        <p>
          <Link to="/docs/intro">Read the documentation</Link>
        </p>
      </main>
    </Layout>
  )
}
