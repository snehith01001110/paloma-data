export const MEDIA_BUCKET = "paloma-establishment-media";
export const MAX_UPLOAD_BYTES = 6 * 1_024 * 1_024;

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const TEMPLATE_PATH_PATTERN =
  /^templates\/v[1-9][0-9]*\/(?:family|category)-[a-z0-9]+(?:-[a-z0-9]+)*\/(hero|card|thumbnail)-([0-9a-f]{16})\.jpg$/;

export type UploadMetadata = {
  objectPath: string;
  sha256: string;
  variant: "hero" | "card" | "thumbnail";
};

export function uploadMetadata(
  objectPathValue: string | null,
  sha256Value: string | null,
): UploadMetadata | null {
  const objectPath = objectPathValue?.trim() ?? "";
  const sha256 = sha256Value?.trim().toLowerCase() ?? "";
  const pathMatch = TEMPLATE_PATH_PATTERN.exec(objectPath);
  if (!pathMatch || !SHA256_PATTERN.test(sha256)) return null;
  if (pathMatch[2] !== sha256.slice(0, 16)) return null;
  return {
    objectPath,
    sha256,
    variant: pathMatch[1] as UploadMetadata["variant"],
  };
}

export function isJpeg(bytes: Uint8Array): boolean {
  return bytes.length >= 4 && bytes[0] === 0xff && bytes[1] === 0xd8 &&
    bytes[bytes.length - 2] === 0xff && bytes[bytes.length - 1] === 0xd9;
}

export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  // Copy into an ArrayBuffer-backed view. TypeScript 5.9 correctly treats a
  // caller's Uint8Array buffer as possibly shared, while Web Crypto requires
  // an ordinary BufferSource.
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new Uint8Array(bytes).buffer,
  );
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

export function encodedObjectPath(objectPath: string): string {
  return objectPath.split("/").map(encodeURIComponent).join("/");
}
