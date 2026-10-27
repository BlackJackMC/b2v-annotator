# b2v-annotator
A tool to annotate ground-truth for Board2vec. Backend made by me and frontend made by Gemini

See [annotation tasks](docs/annotation-tasks.md) for classification and SVG inputs, outputs, shortcuts, and implementation details.

## Folder structure
```
.
├── data: input data
│   ├── keyframes
│   └── obb
└── src
    ├── backend: server to sync progress
    │   ├── app
    │   ├── domain
    │   └── infra
    └── frontend: main application
        ├── core
        └── ui
            ├── components
            └── screens
```
