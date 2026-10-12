const {clearSessionCookie} = require("../../lib/auth");

module.exports = function logout(request, response) {
  if (request.method !== "POST") {
    response.status(405).json({error: "method not allowed"});
    return;
  }
  response.setHeader("Set-Cookie", clearSessionCookie());
  response.status(200).json({ok: true});
};
