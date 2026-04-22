import React from 'react';
import ComponentCreator from '@docusaurus/ComponentCreator';

export default [
  {
    path: '/foresight-mcp/blog',
    component: ComponentCreator('/foresight-mcp/blog', '234'),
    exact: true
  },
  {
    path: '/foresight-mcp/docs',
    component: ComponentCreator('/foresight-mcp/docs', 'd1d'),
    routes: [
      {
        path: '/foresight-mcp/docs',
        component: ComponentCreator('/foresight-mcp/docs', 'c56'),
        routes: [
          {
            path: '/foresight-mcp/docs',
            component: ComponentCreator('/foresight-mcp/docs', 'efc'),
            routes: [
              {
                path: '/foresight-mcp/docs/api/cli-reference',
                component: ComponentCreator('/foresight-mcp/docs/api/cli-reference', 'f33'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/api/overview',
                component: ComponentCreator('/foresight-mcp/docs/api/overview', '9e1'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/api/python-api',
                component: ComponentCreator('/foresight-mcp/docs/api/python-api', 'ebb'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/api/typescript-api',
                component: ComponentCreator('/foresight-mcp/docs/api/typescript-api', '414'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/concepts/blocks',
                component: ComponentCreator('/foresight-mcp/docs/concepts/blocks', 'de5'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/concepts/events',
                component: ComponentCreator('/foresight-mcp/docs/concepts/events', '160'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/concepts/hooks',
                component: ComponentCreator('/foresight-mcp/docs/concepts/hooks', 'a81'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/concepts/memory',
                component: ComponentCreator('/foresight-mcp/docs/concepts/memory', '735'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/concepts/websocket',
                component: ComponentCreator('/foresight-mcp/docs/concepts/websocket', '191'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/examples/basic-usage',
                component: ComponentCreator('/foresight-mcp/docs/examples/basic-usage', 'd1a'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/examples/event-hooks',
                component: ComponentCreator('/foresight-mcp/docs/examples/event-hooks', '506'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/examples/websocket-client',
                component: ComponentCreator('/foresight-mcp/docs/examples/websocket-client', '084'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/guides/managing-blocks',
                component: ComponentCreator('/foresight-mcp/docs/guides/managing-blocks', 'ef1'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/guides/querying-memories',
                component: ComponentCreator('/foresight-mcp/docs/guides/querying-memories', '338'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/guides/real-time-updates',
                component: ComponentCreator('/foresight-mcp/docs/guides/real-time-updates', 'cd8'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/guides/setting-up-hooks',
                component: ComponentCreator('/foresight-mcp/docs/guides/setting-up-hooks', 'a3a'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/guides/storing-memories',
                component: ComponentCreator('/foresight-mcp/docs/guides/storing-memories', 'bf2'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/installation',
                component: ComponentCreator('/foresight-mcp/docs/installation', 'c1a'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/intro',
                component: ComponentCreator('/foresight-mcp/docs/intro', '980'),
                exact: true,
                sidebar: "tutorialSidebar"
              },
              {
                path: '/foresight-mcp/docs/quickstart',
                component: ComponentCreator('/foresight-mcp/docs/quickstart', '9bb'),
                exact: true,
                sidebar: "tutorialSidebar"
              }
            ]
          }
        ]
      }
    ]
  },
  {
    path: '*',
    component: ComponentCreator('*'),
  },
];
