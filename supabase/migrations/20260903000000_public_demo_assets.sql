-- Public, read-only marketing media. Only the service role may upload or
-- replace objects; visitors consume the bucket's stable public URLs.
insert into storage.buckets (
  id,
  name,
  public,
  file_size_limit,
  allowed_mime_types
)
values (
  'public-demo',
  'public-demo',
  true,
  52428800,
  array['video/mp4', 'image/jpeg', 'image/png']
)
on conflict (id) do update set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;
