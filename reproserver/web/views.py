import hmac
from datetime import datetime
from hashlib import sha256
import logging
import mimetypes
import os
import prometheus_client
from sqlalchemy.orm import joinedload
from streaming_form_data import StreamingFormDataParser
from streaming_form_data.targets import ValueTarget
import tempfile
from tornado.escape import utf8
from tornado.web import HTTPError, stream_request_body

from .. import database
from ..repositories import RepositoryError, RepositoryUnknown, \
    get_from_link, get_experiment_from_repository, get_repository_name, \
    get_repository_page_url, parse_repository_url
from .. import rpz_metadata
from ..utils import PromMeasureRequest, background_future
from .base import BaseHandler, HashedFileTarget


logger = logging.getLogger(__name__)


PROM_REQUESTS = PromMeasureRequest(
    count=prometheus_client.Counter(
        'pages_total',
        "Page requests",
        ['name'],
    ),
    time=prometheus_client.Histogram(
        'page_seconds',
        "Page request time",
        ['name'],
    ),
)


class Index(BaseHandler):
    """Landing page from which a user can select an experiment to upload.
    """
    @PROM_REQUESTS.sync('index')
    def get(self):
        return self.render('index.html')

    @PROM_REQUESTS.sync('index')
    def head(self):
        return self.finish()


@stream_request_body
class Upload(BaseHandler):
    """Target of the landing page.

    An experiment has been provided, store it and extract metadata.
    """
    def prepare(self):
        self.request.connection.set_max_body_size(10_000_000_000)
        self.streaming_parser = StreamingFormDataParser(self.request.headers)

        self.uploaded_file_tmp = tempfile.NamedTemporaryFile(prefix='upload_')
        self.uploaded_file = HashedFileTarget(self.uploaded_file_tmp.name)
        self.streaming_parser.register('rpz_file', self.uploaded_file)

        self.rpz_url = ValueTarget()
        self.streaming_parser.register('rpz_url', self.rpz_url)

        self.not_permanent = ValueTarget()
        self.streaming_parser.register('not_permanent', self.not_permanent)

        self.sent_xsrf_token = ValueTarget()
        self.streaming_parser.register('_xsrf', self.sent_xsrf_token)

    def data_received(self, chunk):
        self.streaming_parser.data_received(chunk)

    def check_xsrf_cookie(self):
        pass  # Skip built-in XSRF cookie checking, wait for body

    def check_xsrf_cookie_with_body(self):
        token = self.sent_xsrf_token.value.decode('utf-8', 'replace')
        if not token:
            raise HTTPError(403, "'_xsrf' argument missing from POST")
        _, token, _ = self._decode_xsrf_token(token)
        _, expected_token, _ = self._get_raw_xsrf_token()
        if not token:
            raise HTTPError(403, "'_xsrf' argument has invalid format")
        if not hmac.compare_digest(utf8(token), utf8(expected_token)):
            raise HTTPError(403, "XSRF cookie does not match POST argument")

    @PROM_REQUESTS.async_('upload')
    async def post(self):
        self.check_xsrf_cookie_with_body()

        # If a URL was provided, not a file
        rpz_url = self.rpz_url.value.decode('utf-8', 'replace')
        if rpz_url:
            # Redirect to reproduce_repo view
            try:
                repo, repo_path = await parse_repository_url(rpz_url)
            except RepositoryUnknown:
                if not self.not_permanent.value:
                    return await self.render(
                        'repository_notfound.html',
                        rpz_url=rpz_url,
                    )
                # else: fall through
            except RepositoryError as e:
                self.set_status(404)
                return await self.render(
                    'repository_error.html',
                    message=str(e),
                    rpz_url=rpz_url,
                )
            else:
                return self.redirect(
                    self.reverse_url(
                        'reproduce_repo',
                        repo, repo_path,
                    ),
                    status=303,
                )

            # Fetch and upload URL
            upload = await get_from_link(
                self.db, self.application.object_store, self.request.remote_ip,
                None, None,
                rpz_url, rpz_url,
            )

            # Encode ID for permanent URL
            upload_short_id = upload.short_id

            # Redirect to build page
            return self.redirect(
                self.reverse_url('reproduce_local', upload_short_id),
                status=303,
            )

        # Get uploaded file
        if (
            not os.path.getsize(self.uploaded_file.filename)
            or not self.uploaded_file.multipart_filename
        ):
            return await self.render(
                'setup_badfile.html',
                message="Missing file",
            )
        logger.info("Incoming file: %r", self.uploaded_file.multipart_filename)
        filehash = self.uploaded_file.hasher.hexdigest()
        logger.info("Computed hash: %s", self.uploaded_file.hasher.hexdigest())

        # Check for existence of experiment
        experiment = self.db.query(database.Experiment).get(filehash)
        if experiment:
            experiment.last_access = datetime.utcnow()
            logger.info("File exists in storage")
        else:
            # Insert it in database
            try:
                experiment = await rpz_metadata.make_experiment(
                    filehash,
                    self.uploaded_file.filename,
                )
            except rpz_metadata.InvalidPackage as e:
                return await self.render(
                    'setup_badfile.html',
                    message=str(e),
                )
            self.db.add(experiment)

            # Insert it on S3
            await self.application.object_store.upload_file_async(
                'experiments',
                filehash,
                self.uploaded_file.filename,
            )
            logger.info("Inserted file in storage")

        # Insert Upload in database
        upload = database.Upload(
            experiment=experiment,
            filename=self.uploaded_file.multipart_filename,
            submitted_ip=self.request.remote_ip,
        )
        self.db.add(upload)
        self.db.commit()

        # Encode ID for permanent URL
        upload_short_id = upload.short_id

        # Redirect to build page
        return self.redirect(
            self.reverse_url('reproduce_local', upload_short_id),
            status=303,
        )


class BaseReproduce(BaseHandler):
    def reproduce(self, upload, repo_name=None, repo_url=None):
        experiment = upload.experiment
        filename = upload.filename
        experiment_url = self.url_for_upload(upload)

        input_files = (
            self.db.query(database.Path)
            .filter(database.Path.experiment_hash ==
                    experiment.hash)
            .filter(database.Path.is_input)).all()
        return self.render(
            'setup.html',
            filename=filename,
            built=True, error=False,
            params=experiment.parameters,
            input_files=input_files,
            upload_short_id=upload.short_id,
            experiment_url=experiment_url,
            repo_name=repo_name, repo_url=repo_url,
        )


class ReproduceRepo(BaseReproduce):
    @PROM_REQUESTS.async_('reproduce_repo')
    async def get(self, repo, repo_path):
        """Reproduce an experiment from a data repository.
        """
        # Check the database for an experiment already stored matching the URI
        repository_key = '%s/%s' % (repo, repo_path)
        upload = (
            self.db.query(database.Upload)
            .options(joinedload(database.Upload.experiment))
            .filter(database.Upload.repository_key == repository_key)
            .order_by(database.Upload.id.desc())
        ).first()
        if upload is None:
            try:
                upload = await get_experiment_from_repository(
                    self.db, self.application.object_store,
                    self.request.remote_ip,
                    repo, repo_path,
                )
            except RepositoryError as e:
                self.set_status(404)
                return await self.render('setup_notfound.html', message=str(e))
            except rpz_metadata.InvalidPackage as e:
                self.set_status(404)
                return await self.render('setup_badfile.html', message=str(e))
        else:
            upload.last_access = datetime.utcnow()

        # Also updates last access
        upload.experiment.last_access = datetime.utcnow()
        self.db.commit()

        repo_name = get_repository_name(repo)
        repo_url = await get_repository_page_url(repo, repo_path)
        return await self.reproduce(upload, repo_name, repo_url)


class ReproduceLocal(BaseReproduce):
    @PROM_REQUESTS.sync('reproduce_local')
    def get(self, upload_short_id):
        """Ask for run parameters.
        """
        # Decode info from URL
        try:
            upload_id = database.Upload.decode_id(upload_short_id)
        except ValueError:
            self.set_status(404)
            return self.render('setup_notfound.html')

        # Look up the experiment in database
        upload = (
            self.db.query(database.Upload)
            .options(joinedload(database.Upload.experiment))
            .get(upload_id)
        )
        if upload is None:
            self.set_status(404)
            return self.render('setup_notfound.html')

        # Also updates last access
        upload.last_access = datetime.utcnow()
        upload.experiment.last_access = datetime.utcnow()
        self.db.commit()

        return self.reproduce(upload)


class StartRun(BaseHandler):
    @PROM_REQUESTS.async_('start_run')
    async def post(self, upload_short_id):
        """Gets the run parameters POSTed to from /reproduce.

        Triggers the run and redirects to the results page.
        """
        # Decode info from URL
        try:
            upload_id = database.Upload.decode_id(upload_short_id)
        except ValueError:
            self.set_status(404)
            return await self.render('setup_notfound.html')

        # Look up the experiment in database
        upload = (
            self.db.query(database.Upload)
            .options(joinedload(database.Upload.experiment))
            .get(upload_id)
        )
        if upload is None:
            self.set_status(404)
            return await self.render('setup_notfound.html')
        experiment = upload.experiment

        # Update last access
        upload.last_access = datetime.utcnow()
        upload.experiment.last_access = datetime.utcnow()

        # New run entry
        run = database.Run(experiment_hash=experiment.hash,
                           upload_id=upload_id,
                           submitted_ip=self.request.remote_ip)
        self.db.add(run)

        # Get list of parameters
        params = set()
        params_unset = set()
        for param in experiment.parameters:
            if not param.optional:
                params_unset.add(param.name)
            params.add(param.name)

        # Get run parameters
        for k, v in self.request.body_arguments.items():
            if k.startswith('param_'):
                if not v:
                    continue
                name = k[6:]
                if name not in params:
                    raise ValueError("Unknown parameter %s" % k)
                v = v[-1].decode('utf-8')
                run.parameter_values.append(
                    database.ParameterValue(name=name, value=v)
                )
                params_unset.discard(name)

        if params_unset:
            raise ValueError("Missing value for parameters: %s" %
                             ", ".join(params_unset))

        # Get list of input files
        input_files = set(
            p.name for p in (
                self.db.query(database.Path)
                .filter(database.Path.experiment_hash == experiment.hash)
                .filter(database.Path.is_input)
            ).all())

        # Get input files
        for k, uploaded_file in self.request.files.items():
            if not uploaded_file:
                continue
            uploaded_file = uploaded_file[0]

            if not k.startswith('inputfile_') or k[10:] not in input_files:
                raise ValueError("Unknown input file %s" % k)

            name = k[10:]
            logger.info("Incoming input file: %s", name)

            # Hash file
            hasher = sha256(uploaded_file.body)
            inputfilehash = hasher.hexdigest()
            logger.info("Computed hash: %s", inputfilehash)

            # Insert it into S3
            await self.application.object_store.upload_bytes_async(
                'inputs',
                inputfilehash,
                uploaded_file.body,
            )
            logger.info("Inserted file in storage")

            # Insert it in database
            input_file = database.InputFile(
                hash=inputfilehash, name=name,
                size=len(uploaded_file.body),
            )
            run.input_files.append(input_file)

        # Get ports to expose
        for port_str in self.get_body_argument('ports', '').split():
            port_str = port_str.strip()
            if port_str:
                try:
                    port = int(port_str)
                    if not (1 <= port <= 65535):
                        raise ValueError
                except (ValueError, OverflowError):
                    raise ValueError("Invalid port number %r" % port_str)
                run.ports.append(database.RunPort(
                    port_number=port,
                ))

        # Trigger run
        self.db.commit()
        background_future(self.application.runner.run(run.id))

        # Redirect to results page
        return self.redirect(
            self.reverse_url('results', run.short_id),
            status=303,
        )


class Results(BaseHandler):
    @PROM_REQUESTS.sync('results')
    def get(self, run_short_id):
        """Shows the results of a run, whether it's done or in progress.
        """
        # Decode info from URL
        try:
            run_id = database.Run.decode_id(run_short_id)
        except ValueError:
            self.set_status(404)
            return self.render('results_notfound.html')

        # Look up the run in the database
        run = (
            self.db.query(database.Run)
            .options(joinedload(database.Run.experiment),
                     joinedload(database.Run.upload),
                     joinedload(database.Run.parameter_values),
                     joinedload(database.Run.input_files),
                     joinedload(database.Run.output_files))
        ).get(run_id)
        if run is None:
            self.set_status(404)
            return self.render('results_notfound.html')
        # Update last access
        run.experiment.last_access = datetime.utcnow()
        self.db.commit()

        def get_port_url(port_number):
            tpl = os.environ.get(
                'WEB_PROXY_URL',
                'http://{short_id}-{port}.127.0.0.1.nip.io:8001',
            )
            return tpl.format(
                short_id=run_short_id,
                port=port_number,
            )

        def output_link(output_file):
            experiment_hash = output_file.run.experiment_hash
            path = self.db.query(database.Path).filter(
                database.Path.experiment_hash == experiment_hash,
                database.Path.name == output_file.name,
            ).one().path
            mime = mimetypes.guess_type(path)[0]
            return self.application.object_store.presigned_serve_url(
                'outputs', output_file.hash,
                output_file.name,
                mime,
            )

        return self.render(
            'results.html',
            run=run,
            log=run.get_log(0),
            started=bool(run.started),
            done=bool(run.done),
            experiment_url=self.url_for_upload(run.upload),
            get_port_url=get_port_url,
            output_link=output_link,
        )


class ResultsJson(BaseHandler):
    @PROM_REQUESTS.sync('results-json')
    def get(self, run_short_id):
        # Decode info from URL
        try:
            run_id = database.Run.decode_id(run_short_id)
        except ValueError:
            return self.send_error_json(404, "Not found")

        # Look up the run in the database
        run = (
            self.db.query(database.Run)
            .options(joinedload(database.Run.experiment),
                     joinedload(database.Run.upload),
                     joinedload(database.Run.parameter_values),
                     joinedload(database.Run.input_files),
                     joinedload(database.Run.output_files))
        ).get(run_id)
        if run is None:
            return self.send_error_json(404, "Not found")

        log_from = int(self.get_query_argument('log_from', '0'), 10)
        return self.send_json({
            'started': bool(run.started),
            'done': bool(run.done),
            'log': run.get_log(log_from),
        })


class About(BaseHandler):
    @PROM_REQUESTS.sync('about')
    def get(self):
        return self.render('about.html')


class Data(BaseHandler):
    """Print some system information.
    """
    @PROM_REQUESTS.sync('data')
    def get(self):
        return self.render(
            'data.html',
            experiments=self.db.query(database.Experiment).all(),
        )


class Health(BaseHandler):
    @PROM_REQUESTS.sync('health')
    async def get(self):
        self.set_header('Content-Type', 'text/plain')

        # We're not ready if we've been asked to shut down
        if self.application.is_exiting:
            self.set_status(503, "Shutting down")
            return await self.finish('Shutting down')

        # Health checks
        checks = [
            await self.application.object_store.check(),
            database.check(self.application.DBSession),
        ]
        errors = [c for c in checks if c]
        if errors:
            self.set_status(503)
            return await self.finish('\n'.join(errors))

        return await self.finish('Ok')
