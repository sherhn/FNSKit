<#import "template.ftl" as layout>
<@layout.emailLayout title=msg("fnskitVerifyTitle")>
  <p>${msg("fnskitVerifyLead", realmName)}</p>
  <@layout.button href=link label=msg("fnskitVerifyButton") />
  <@layout.linkNote expiration=linkExpirationFormatter(linkExpiration) />
  <p style="margin:0;font-size:13px;color:#5C5C58;">${msg("fnskitVerifyIgnore")}</p>
</@layout.emailLayout>
