module.exports = function logout(request, response) {
  if (request.method !== "POST") {
    response.status(405).json({error: "method not allowed"});
    return;
  }
  response.setHeader("Set-Cookie", "cloud_access=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0");
  response.status(200).json({ok: true});
};
