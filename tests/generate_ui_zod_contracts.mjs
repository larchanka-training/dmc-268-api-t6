import { mkdir, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const uiDir = resolve(process.env.DMC_268_UI_DIR ?? '')
const outputPath = resolve(
  process.env.OUTPUT_PATH ?? 'tests/fixtures/ui_zod_contracts.json',
)

if (!process.env.DMC_268_UI_DIR) {
  throw new Error('DMC_268_UI_DIR must point to a checkout of dmc-268-ui-t6')
}

const { z } = await import(pathToFileURL(resolve(uiDir, 'node_modules/zod/index.js')).href)
const { RunActionSchema, RunSessionSchema } = await import(
  pathToFileURL(resolve(uiDir, 'src/entities/run/model/schemas.ts')).href,
)
const { ReviewCommentSchema } = await import(
  pathToFileURL(resolve(uiDir, 'src/entities/review/model/schemas.ts')).href,
)

const commit = (await import('node:child_process')).execFileSync(
  'git',
  ['-C', uiDir, 'rev-parse', 'HEAD'],
  { encoding: 'utf8' },
).trim()

const contracts = {
  provenance: {
    repository: 'https://github.com/larchanka-training/dmc-268-ui-t6',
    commit,
    generator: 'z.toJSONSchema (Zod 4)',
    command:
      'node --experimental-strip-types tests/generate_ui_zod_contracts.mjs',
  },
  schemas: {
    runSession: z.toJSONSchema(RunSessionSchema),
    runAction: z.toJSONSchema(RunActionSchema),
    reviewComment: z.toJSONSchema(ReviewCommentSchema),
  },
}

await mkdir(dirname(outputPath), { recursive: true })
await writeFile(outputPath, `${JSON.stringify(contracts, null, 2)}\n`)
