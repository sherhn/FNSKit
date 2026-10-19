// Минификация фронтендового JS. Имена файлов сохраняются, поэтому пути в
// разметке не меняются: в образ вместо js/ уезжает dist/js/ с тем же составом.
//
// Source maps намеренно не пишем — иначе минификация теряет смысл.
import { copyFile, mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { minify } from "terser";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const from = join(root, "js");
const to = join(root, "dist", "js");
const vendor = join(root, "dist", "vendor");

await mkdir(to, { recursive: true });
await mkdir(vendor, { recursive: true });

const files = (await readdir(from)).filter((name) => name.endsWith(".js"));
if (files.length === 0) throw new Error("В каталоге js/ нет файлов для сборки");

for (const name of files) {
  const source = await readFile(join(from, name), "utf8");
  const result = await minify(source, {
    ecma: 2020,
    compress: { passes: 2 },
    mangle: true,
    format: { comments: false }
  });

  if (typeof result.code !== "string") throw new Error(`Terser не вернул код для ${name}`);

  await writeFile(join(to, name), result.code, "utf8");
  const ratio = Math.round((1 - result.code.length / source.length) * 100);
  console.log(`${name}: ${source.length} -> ${result.code.length} байт (-${ratio}%)`);
}

// Keep document parsers under the site's own origin. Their exact package
// versions are locked in package-lock.json, so a CDN cannot swap code seen by
// an editor user.
await copyFile(join(root, "node_modules", "utif", "UTIF.js"), join(vendor, "utif-3.1.0.js"));
await copyFile(join(root, "node_modules", "pdfjs-dist", "build", "pdf.mjs"), join(vendor, "pdfjs-6.1.200.mjs"));
await copyFile(join(root, "node_modules", "pdfjs-dist", "build", "pdf.worker.mjs"), join(vendor, "pdfjs-worker-6.1.200.mjs"));
// Клиент входа для кабинета: один ES-модуль без зависимостей.
await copyFile(join(root, "node_modules", "keycloak-js", "lib", "keycloak.js"), join(vendor, "keycloak-js-26.2.4.mjs"));
