import { execFileSync } from 'node:child_process'
import { mkdir, writeFile } from 'node:fs/promises'
import { registerHooks } from 'node:module'
import { dirname, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

// DMC_268_UI_DIR: a clean ui checkout after `pnpm install`; OUTPUT_PATH: where to write the
// snapshot (default: the committed fixture).
const uiDir = resolve(process.env.DMC_268_UI_DIR ?? '')
const outputPath = resolve(
  process.env.OUTPUT_PATH ?? 'tests/fixtures/ui_zod_contracts.json',
)

if (!process.env.DMC_268_UI_DIR) {
  throw new Error('DMC_268_UI_DIR must point to a checkout of dmc-268-ui-t6')
}

// The ui imports its own modules without an extension (`../../review/model/schemas`), which
// Vite resolves and Node does not: retry a failed relative specifier as `.ts`. The schema files
// import only files, never directories, so no `/index.ts` fallback is needed.
registerHooks({
  resolve(specifier, context, nextResolve) {
    try {
      return nextResolve(specifier, context)
    } catch (error) {
      const relative = specifier.startsWith('./') || specifier.startsWith('../')
      if (!relative || error.code !== 'ERR_MODULE_NOT_FOUND') {
        throw error
      }
      try {
        return nextResolve(`${specifier}.ts`, context)
      } catch {
        throw error
      }
    }
  },
})

const load = (path) => import(pathToFileURL(resolve(uiDir, path)).href)

const { z } = await load('node_modules/zod/index.js')
const diff = await load('src/entities/diff/model/schemas.ts')
const repository = await load('src/entities/repository/model/schemas.ts')
const review = await load('src/entities/review/model/schemas.ts')
const run = await load('src/entities/run/model/schemas.ts')
const user = await load('src/entities/user/model/schemas.ts')

const git = (...args) => execFileSync('git', ['-C', uiDir, ...args], { encoding: 'utf8' }).trim()
// provenance.commit must describe exactly the schemas that were exported.
if (git('status', '--porcelain', '--untracked-files=no') !== '') {
  throw new Error(`${uiDir} has uncommitted changes: commit or stash them first`)
}
const commit = git('rev-parse', 'HEAD')

// Keys are the snapshot names the api tests read; each maps to one ui Zod schema.
const sources = {
  runSession: run.RunSessionSchema,
  runAction: run.RunActionSchema,
  reviewComment: review.ReviewCommentSchema,
  runDetail: run.RunDetailSchema,
  findingView: review.FindingViewSchema,
  runListPage: run.RunListPageSchema,
  runUpdatedEvent: run.RunUpdatedEventSchema,
  repository: repository.RepositorySchema,
  repositoryUpdate: repository.UpdateRepositorySchema,
  rawFileDiff: diff.RawFileDiffSchema,
  fileSlice: diff.FileSliceSchema,
  authSession: user.AuthSessionSchema,
  me: user.MeSchema,
}

const contracts = {
  provenance: {
    repository: 'https://github.com/larchanka-training/dmc-268-ui-t6',
    commit,
    generator: 'z.toJSONSchema (Zod 4)',
    command:
      'DMC_268_UI_DIR=<ui checkout after pnpm install> node tests/generate_ui_zod_contracts.mjs',
  },
  schemas: Object.fromEntries(
    Object.entries(sources).map(([name, schema]) => [name, z.toJSONSchema(schema)]),
  ),
}

await mkdir(dirname(outputPath), { recursive: true })
await writeFile(outputPath, `${JSON.stringify(contracts, null, 2)}\n`)
