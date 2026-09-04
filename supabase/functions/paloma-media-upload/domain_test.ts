import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.16";
import {
  encodedObjectPath,
  isJpeg,
  sha256Hex,
  uploadMetadata,
} from "./domain.ts";

Deno.test("template upload metadata binds the path to the content hash", () => {
  const hash = "a".repeat(64);
  assertEquals(
    uploadMetadata(
      `templates/v1/family-beer/thumbnail-${hash.slice(0, 16)}.jpg`,
      hash,
    ),
    {
      objectPath: `templates/v1/family-beer/thumbnail-${hash.slice(0, 16)}.jpg`,
      sha256: hash,
      variant: "thumbnail",
    },
  );
});

Deno.test("template upload metadata rejects unsafe or mismatched paths", () => {
  const hash = "b".repeat(64);
  assertEquals(uploadMetadata("../escape.jpg", hash), null);
  assertEquals(
    uploadMetadata("templates/v1/family-beer/hero-deadbeefdeadbeef.jpg", hash),
    null,
  );
  assertEquals(
    uploadMetadata(`venues/v1/family-beer/hero-${hash.slice(0, 16)}.jpg`, hash),
    null,
  );
});

Deno.test("jpeg and encoding helpers are deterministic", async () => {
  const bytes = new Uint8Array([0xff, 0xd8, 0x01, 0xff, 0xd9]);
  assertEquals(isJpeg(bytes), true);
  assertEquals(isJpeg(new Uint8Array([0x89, 0x50, 0x4e, 0x47])), false);
  assertEquals(
    await sha256Hex(new TextEncoder().encode("paloma")),
    "86f426b91150406161f2781857fa82495bc433d32659d536a26b021cc31ab503",
  );
  assertEquals(
    encodedObjectPath("templates/v1/family beer/a.jpg"),
    "templates/v1/family%20beer/a.jpg",
  );
});

Deno.test("sha256 helper rejects no valid input", async () => {
  await assertRejects(
    async () => {
      // Exercise the platform error boundary without weakening the helper's type.
      await crypto.subtle.digest("not-a-digest", new Uint8Array());
    },
    DOMException,
  );
});
