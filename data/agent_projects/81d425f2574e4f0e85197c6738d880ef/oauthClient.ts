import dotenv from "dotenv";
dotenv.config();

import { OAuth2Client } from "google-auth-library";

const oauthClient = new OAuth2Client({
  clientId: process.env.GOOGLE_CLIENT_ID,
  clientSecret: process.env.GOOGLE_CLIENT_SECRET,
  redirectUri: process.env.GOOGLE_REDIRECT_URI,
});

export default oauthClient;
