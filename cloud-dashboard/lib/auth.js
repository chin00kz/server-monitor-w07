function getAccessToken(request) {
  const cookies = request.headers.cookie || "";
  const cookie = cookies.split(";").map(value => value.trim()).find(value => value.startsWith("cloud_access="));
  if (!cookie) return null;
  try {
    return decodeURIComponent(cookie.slice("cloud_access=".length));
  } catch (error) {
    return null;
  }
}

module.exports = {getAccessToken};
