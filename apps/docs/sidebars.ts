import type { SidebarsConfig } from '@docusaurus/plugin-content-docs'

const sidebars: SidebarsConfig = {
  tutorialSidebar: [
    {
      type: 'category',
      label: 'Getting Started',
      items: ['intro', 'quickstart', 'installation'],
    },
    {
      type: 'category',
      label: 'Core Concepts',
      items: [
        'concepts/memory',
        'concepts/blocks',
        'concepts/events',
        'concepts/hooks',
        'concepts/websocket',
      ],
    },
    {
      type: 'category',
      label: 'Guides',
      items: [
        'guides/storing-memories',
        'guides/querying-memories',
        'guides/managing-blocks',
        'guides/setting-up-hooks',
        'guides/real-time-updates',
      ],
    },
    {
      type: 'category',
      label: 'API Reference',
      items: [
        'api/overview',
        'api/python-api',
        'api/typescript-api',
        'api/cli-reference',
      ],
    },
    {
      type: 'category',
      label: 'Examples',
      items: [
        'examples/basic-usage',
        'examples/event-hooks',
        'examples/websocket-client',
      ],
    },
  ],
}

export default sidebars
